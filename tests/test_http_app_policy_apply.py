import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    cast,
)
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule
from control_plane.http_app import (
    AcceptedEvidenceResponse,
    create_launchplane_fastapi_app,
    idempotency_scope,
    store_product_config_dry_run_record,
)
from control_plane.product_config_http import ProductConfigApplyResponse
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
)
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.postgres import DbOnlyMutationRequest
from control_plane.storage.postgres import ProductProfileCompareWriteResult
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from tests.http_app_test_support import (
    _AGENT_WRITE_INTENT_SOURCE_URL,
    _agent_write_intent_payload,
    _agent_write_intent_policy,
    _AgentWriteIntentEvaluateReplayOnlyStore,
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _ConcurrentProductConfigDryRunMarkerStore,
    _every_code_notification_policy_record,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_human_runtime_key_safety_policy_apply_policy,
    _github_oauth_config,
    _local_operator_bearer_config,
    _MissingProductReadStore,
    _notification_policy_apply_policy,
    _post_agent_write_intent_evaluate,
    _post_context_cutover_apply,
    _post_legacy_context_cleanup_apply,
    _post_product_config_apply,
    _post_product_environment_config_apply,
    _post_runtime_key_safety_policy_apply,
    _preview_pr_feedback_notification_policy_record,
    _product_config_policy,
    _product_profile_read_policy,
    _product_profile_write_policy,
    _ProductContextApplyReplayOnlyStore,
    _public_ingress_notification_policy_record,
    _RejectingVerifier,
    _runtime_key_safety_policy_apply_payload,
    _runtime_key_safety_policy_apply_policy,
    _seed_agent_write_intent_secret_binding,
    _terminal_agent_write_intent_policy,
    _write_context_cutover_audit_records,
)
from tests.support.http import lifespan_client
from tests.support.auth import _identity, _local_operator_policy, _StubVerifier
from tests.support.product_config import (
    _meta_product_config_payload,
    _product_config_payload,
    _product_config_secrets,
)
from tests.support.product_reads import _get_context_cutover_audit
from tests.support.profiles import _generic_site_profile_payload, _product_profile_payload_with_prod
from tests.support.stores import (
    _seed_tracked_target_records,
    _sqlite_database_url,
    _write_runtime_key_safety_policy,
)


class FastApiNotificationPolicyApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_ingress_notification_policy_apply_writes_db_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                policy_record = _public_ingress_notification_policy_record()

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "public-ingress-notification-policy-test",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_public_ingress_notification_policy_records(
                    product="launchplane", context_name="launchplane", status="enabled"
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {"public_ingress_notification_policy_id": policy_record.policy_id},
        )
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertTrue(payload["result"]["changed"])
        self.assertEqual(
            payload["result"]["policy"]["reminder_interval_seconds"],
            policy_record.reminder_interval_seconds,
        )
        self.assertEqual(records, (policy_record,))

    async def test_public_ingress_notification_policy_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _public_ingress_notification_policy_record(
                            policy_id="public-ingress-notification-launchplane-dry-run"
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_public_ingress_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(records, ())

    async def test_public_ingress_notification_policy_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _public_ingress_notification_policy_record().model_dump(mode="json"),
                }

                async with lifespan_client(app) as client:
                    first_response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/public-ingress/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "public-ingress-notification-policy-replay",
                        },
                        payload=payload,
                    )
                    second_response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/public-ingress/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "public-ingress-notification-policy-replay",
                        },
                        payload=payload,
                    )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_public_ingress_notification_policy_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _public_ingress_notification_policy_record().model_dump(mode="json"),
                }
                conflicting_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _public_ingress_notification_policy_record(
                        policy_id="public-ingress-notification-launchplane-conflict"
                    ).model_dump(mode="json"),
                }

                async with lifespan_client(app) as client:
                    await _asgi_request(
                        client,
                        "POST",
                        "/v1/public-ingress/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "public-ingress-notification-policy-conflict",
                        },
                        payload=first_payload,
                    )
                    response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/public-ingress/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "public-ingress-notification-policy-conflict",
                        },
                        payload=conflicting_payload,
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_every_code_notification_policy_apply_writes_db_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                policy_record = _every_code_notification_policy_record()

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "every-code-notification-policy-test",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_every_code_notification_policy_records(
                    repository="cbusillo/code", status="enabled"
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(
            payload["records"],
            {"every_code_notification_policy_id": policy_record.policy_id},
        )
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(records, (policy_record,))

    async def test_every_code_notification_policy_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _every_code_notification_policy_record(
                            policy_id="every-code-notification-launchplane-dry-run"
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_every_code_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(records, ())

    async def test_every_code_notification_policy_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _every_code_notification_policy_record().model_dump(mode="json"),
                }

                async with lifespan_client(app) as client:
                    first_response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/every-code/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "every-code-notification-policy-replay",
                        },
                        payload=payload,
                    )
                    second_response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/every-code/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "every-code-notification-policy-replay",
                        },
                        payload=payload,
                    )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_every_code_notification_policy_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _every_code_notification_policy_record().model_dump(mode="json"),
                }
                conflicting_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _every_code_notification_policy_record(
                        policy_id="every-code-notification-launchplane-conflict"
                    ).model_dump(mode="json"),
                }

                async with lifespan_client(app) as client:
                    await _asgi_request(
                        client,
                        "POST",
                        "/v1/every-code/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "every-code-notification-policy-conflict",
                        },
                        payload=first_payload,
                    )
                    response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/every-code/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "every-code-notification-policy-conflict",
                        },
                        payload=conflicting_payload,
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_public_ingress_notification_policy_rejects_mismatched_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="other-product",
                        context="other-context",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _public_ingress_notification_policy_record().model_dump(
                            mode="json"
                        ),
                    },
                )
                records = store.list_public_ingress_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_every_code_notification_policy_rejects_mismatched_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="other-product",
                        context="other-context",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _every_code_notification_policy_record().model_dump(mode="json"),
                    },
                )
                records = store.list_every_code_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_preview_pr_feedback_notification_policy_apply_writes_db_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )
                policy_record = _preview_pr_feedback_notification_policy_record()

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-policy-test",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_preview_pr_feedback_notification_policy_records(
                    product="sellyouroutboard",
                    context_name="sellyouroutboard",
                    repository="cbusillo/sellyouroutboard",
                    status="enabled",
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(
            payload["records"],
            {"preview_pr_feedback_notification_policy_id": policy_record.policy_id},
        )
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(records, (policy_record,))

    async def test_preview_pr_feedback_notification_policy_replays_idempotent_response(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _preview_pr_feedback_notification_policy_record().model_dump(
                        mode="json"
                    ),
                }

                async with lifespan_client(app) as client:
                    first_response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/previews/pr-feedback/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "preview-pr-feedback-notification-policy-replay",
                        },
                        payload=payload,
                    )
                    second_response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/previews/pr-feedback/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "preview-pr-feedback-notification-policy-replay",
                        },
                        payload=payload,
                    )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_preview_pr_feedback_notification_policy_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _preview_pr_feedback_notification_policy_record().model_dump(
                        mode="json"
                    ),
                }
                conflicting_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _preview_pr_feedback_notification_policy_record(
                        policy_id="preview-pr-feedback-notification-syo-conflict"
                    ).model_dump(mode="json"),
                }

                async with lifespan_client(app) as client:
                    await _asgi_request(
                        client,
                        "POST",
                        "/v1/previews/pr-feedback/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "preview-pr-feedback-notification-policy-conflict",
                        },
                        payload=first_payload,
                    )
                    response = await _asgi_request(
                        client,
                        "POST",
                        "/v1/previews/pr-feedback/notification-policies/apply",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "preview-pr-feedback-notification-policy-conflict",
                        },
                        payload=conflicting_payload,
                    )
            finally:
                store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_preview_pr_feedback_notification_policy_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _preview_pr_feedback_notification_policy_record(
                            policy_id="preview-pr-feedback-notification-syo-dry-run"
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_preview_pr_feedback_notification_policy_records(
                    product="sellyouroutboard",
                    context_name="sellyouroutboard",
                    repository="cbusillo/sellyouroutboard",
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(records, ())

    async def test_preview_pr_feedback_notification_policy_rejects_wildcard_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-global",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _preview_pr_feedback_notification_policy_record(
                            policy_id="preview-pr-feedback-notification-global",
                            product="",
                            context="",
                            repository="",
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_preview_pr_feedback_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_policy_scope")
        self.assertEqual(records, ())

    async def test_preview_pr_feedback_notification_policy_rejects_mismatched_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-denied",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _preview_pr_feedback_notification_policy_record().model_dump(
                            mode="json"
                        ),
                    },
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_notification_policy_apply_local_operator_requires_reason(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_local_operator_policy(
                        actions=(
                            "public_ingress_notification_policy.apply",
                            "every_code_notification_policy.apply",
                            "preview_pr_feedback_notification_policy.apply",
                        ),
                        products=("launchplane", "sellyouroutboard"),
                        contexts=("launchplane", "sellyouroutboard"),
                        token_label="local-owner-read",
                    ),
                    record_store_factory=lambda: store,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                cases = (
                    (
                        "/v1/public-ingress/notification-policies/apply",
                        _public_ingress_notification_policy_record(),
                    ),
                    (
                        "/v1/every-code/notification-policies/apply",
                        _every_code_notification_policy_record(),
                    ),
                    (
                        "/v1/previews/pr-feedback/notification-policies/apply",
                        _preview_pr_feedback_notification_policy_record(),
                    ),
                )

                responses = []
                for path, policy_record in cases:
                    responses.append(
                        await _asgi_request(
                            app,
                            "POST",
                            path,
                            headers={"Authorization": "Bearer local-operator-token"},
                            payload={
                                "schema_version": 1,
                                "mode": "apply",
                                "policy": policy_record.model_dump(mode="json"),
                            },
                        )
                    )
            finally:
                store.close()

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "reason_required")

    async def test_notification_policy_apply_accepts_local_operator_reason(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_local_operator_policy(
                        actions=("public_ingress_notification_policy.apply",),
                        products=("launchplane",),
                        contexts=("launchplane",),
                        token_label="local-owner-read",
                    ),
                    record_store_factory=lambda: store,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                policy_record = _public_ingress_notification_policy_record(
                    policy_id="public-ingress-notification-local-operator"
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "public-ingress-local-operator-apply",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "reason": "Enable local operator ingress notifications.",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_public_ingress_notification_policy_records(
                    product="launchplane", context_name="launchplane", status="enabled"
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(records, (policy_record,))

    async def test_notification_policy_apply_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="public_ingress_notification_policy.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/public-ingress/notification-policies/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "schema_version": 1,
                    "mode": "dry-run",
                    "policy": _public_ingress_notification_policy_record().model_dump(mode="json"),
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")

    async def test_openapi_includes_notification_policy_apply_routes(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/v1/public-ingress/notification-policies/apply", paths)
        self.assertIn("/v1/every-code/notification-policies/apply", paths)
        self.assertIn("/v1/previews/pr-feedback/notification-policies/apply", paths)


class FastApiRuntimeKeySafetyPolicyApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_key_safety_policy_apply_reconciles_rules_and_replays(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                _write_runtime_key_safety_policy(database_url=database_url)
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_runtime_key_safety_policy_apply_policy(
                        action="runtime_key_safety.write"
                    ),
                    record_store_factory=lambda: store,
                )
                payload = _runtime_key_safety_policy_apply_payload()

                first_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    payload,
                    idempotency_key="runtime-key-safety-policy:test",
                )
                active_policy = store.list_runtime_key_safety_policy_records(
                    status="active", limit=1
                )[0]
                replay_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    payload,
                    idempotency_key="runtime-key-safety-policy:test",
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        first_payload = first_response.json()
        self.assertEqual(first_payload["status"], "accepted")
        self.assertEqual(
            set(first_payload["records"]),
            {"runtime_key_safety_policy_record_id"},
        )
        self.assertEqual(
            first_payload["records"]["runtime_key_safety_policy_record_id"],
            active_policy.record_id,
        )
        self.assertEqual(first_payload["result"]["changed"], True)
        self.assertEqual(
            first_payload["result"]["runtime_key_safety_policy"]["binding_keys"],
            ["RESEND_API_KEY", "SMTP_PASSWORD"],
        )
        self.assertEqual(
            [rule.binding_key for rule in active_policy.rules],
            ["RESEND_API_KEY", "SMTP_PASSWORD"],
        )
        self.assertEqual(replay_response.status_code, 202)
        replay_payload = replay_response.json()
        self.assertEqual(replay_payload["records"], first_payload["records"])
        self.assertEqual(replay_payload["result"], first_payload["result"])
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["original_trace_id"], first_payload["trace_id"])

    async def test_runtime_key_safety_policy_apply_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_runtime_key_safety_policy_apply_policy(
                        action="runtime_key_safety.write"
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = _runtime_key_safety_policy_apply_payload()
                conflicting_payload = _runtime_key_safety_policy_apply_payload()
                conflicting_rules = cast(list[dict[str, object]], conflicting_payload["rules"])
                conflicting_rules[1] = {
                    "binding_key": "MAILGUN_API_KEY",
                    "secret_class": "prod_only",
                    "allowed_contexts": ["sellyouroutboard"],
                    "allowed_instances": ["prod"],
                }

                first_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    first_payload,
                    idempotency_key="runtime-key-safety-policy:conflict",
                )
                second_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    conflicting_payload,
                    idempotency_key="runtime-key-safety-policy:conflict",
                )
                active_policy = store.list_runtime_key_safety_policy_records(
                    status="active", limit=1
                )[0]
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        self.assertEqual(
            [rule.binding_key for rule in active_policy.rules],
            ["RESEND_API_KEY", "SMTP_PASSWORD"],
        )

    async def test_runtime_key_safety_policy_apply_rejects_without_permission(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_runtime_key_safety_policy_apply_policy(
                        action="product_profile.read"
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _post_runtime_key_safety_policy_apply(
                    app,
                    _runtime_key_safety_policy_apply_payload(),
                    idempotency_key="runtime-key-safety-policy:unauthorized",
                )
                active_records = store.list_runtime_key_safety_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(active_records, ())

    async def test_runtime_key_safety_policy_apply_rejects_human_session_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            oauth_config = _github_oauth_config()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=_github_human_runtime_key_safety_policy_apply_policy(),
                    record_store_factory=lambda: store,
                    human_session_manager=session_manager,
                )

                response = await _post_runtime_key_safety_policy_apply(
                    app,
                    _runtime_key_safety_policy_apply_payload(),
                    authorization="",
                    headers={"Cookie": session_manager.session_cookie_header(human_session)},
                )
                active_records = store.list_runtime_key_safety_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(active_records, ())

    async def test_runtime_key_safety_policy_apply_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_runtime_key_safety_policy_apply_policy(
                    action="runtime_key_safety.write"
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_runtime_key_safety_policy_apply(
                app,
                _runtime_key_safety_policy_apply_payload(),
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_openapi_includes_runtime_key_safety_policy_apply_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/runtime-key-safety/policies/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_runtime_key_safety_policy")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RuntimeKeySafetyPolicyApplyEnvelope",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route["responses"][status_code]))


class FastApiAgentWriteIntentEvaluateTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluate_returns_allowed_dry_run_without_execution(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                    reason="Check whether rerun can be requested safely.",
                ),
                idempotency_key="agent-write-intent-rerun",
            )
            record_pointer = response.json()["result"]["record"]
            record = store.read_agent_write_intent_record(record_pointer["record_id"])

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {})
        intent = payload["result"]["intent"]
        self.assertEqual(intent["status"], "allowed")
        self.assertEqual(intent["authz_action"], "every_code_work_request.rerun")
        self.assertFalse(intent["safe_to_execute"])
        self.assertEqual(intent["reason_code"], "authorized")
        self.assertEqual(intent["audit"]["decision"], "allowed")
        self.assertEqual(intent["audit"]["subject"]["action_safety"], "safe_write")
        self.assertEqual(record.evaluation.status, "allowed")
        self.assertEqual(record.evaluation.intent, "every_code_rerun")
        self.assertEqual(record.idempotency_key, "agent-write-intent-rerun")
        self.assertEqual(record.request.source_url, _AGENT_WRITE_INTENT_SOURCE_URL)
        self.assertEqual(record.trace_id, payload["trace_id"])

    async def test_evaluate_replays_idempotent_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )
            payload = _agent_write_intent_payload(
                intent="every_code_rerun",
                mode="dry_run",
                product="launchplane",
                context="launchplane",
            )

            first_response = await _post_agent_write_intent_evaluate(
                app,
                payload,
                idempotency_key="agent-write-intent-replay",
            )
            second_response = await _post_agent_write_intent_evaluate(
                app,
                payload,
                idempotency_key="agent-write-intent-replay",
            )
            records = store.list_agent_write_intent_records(
                product="launchplane",
                context_name="launchplane",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        second_payload = second_response.json()
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_response.json()["trace_id"])
        self.assertEqual(len(records), 1)

    async def test_evaluate_replays_before_write_store_check(self) -> None:
        payload = _agent_write_intent_payload(
            intent="every_code_rerun",
            mode="dry_run",
            product="launchplane",
            context="launchplane",
        )
        record_store = _AgentWriteIntentEvaluateReplayOnlyStore(
            payload=payload,
            idempotency_key="agent-write-intent-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_agent_write_intent_policy(
                actions=("every_code_work_request.rerun",),
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: record_store,
        )

        response = await _post_agent_write_intent_evaluate(
            app,
            payload,
            idempotency_key="agent-write-intent-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(record_store.read_idempotency_calls, 1)

    async def test_evaluate_rejects_reused_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            first_response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                ),
                idempotency_key="agent-write-intent-reused",
            )
            conflict_response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                    reason="Different request reason.",
                ),
                idempotency_key="agent-write-intent-reused",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_evaluate_denies_ungranted_intent_as_preflight_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="promotion_dispatch",
                    mode="apply",
                    product="verireel",
                    context="verireel",
                    reason="Request prod promotion dispatch.",
                ),
            )
            record = store.read_agent_write_intent_record(
                response.json()["result"]["record"]["record_id"]
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "authorization_denied")
        self.assertFalse(intent["safe_to_execute"])
        self.assertEqual(intent["audit"]["decision"], "denied")
        self.assertEqual(intent["audit"]["subject"]["action_safety"], "prod")
        self.assertEqual(record.evaluation.status, "denied")

    async def test_evaluate_requires_dry_run_for_config_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply",),
                    product="verireel",
                    context="verireel-testing",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="apply",
                    product="verireel",
                    context="verireel-testing",
                    reason="Apply product config after review.",
                ),
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "dry_run_required")
        self.assertFalse(intent["safe_to_execute"])
        self.assertEqual(intent["audit"]["reason_code"], "dry_run_required")

    async def test_terminal_agent_evaluate_checks_scoped_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_write_intent_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                    reason="Check whether local agent can request a rerun.",
                ),
                authorization="Bearer terminal-read-token",
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "allowed")
        self.assertEqual(intent["audit"]["subject"]["subject_type"], "terminal_agent")
        self.assertFalse(intent["audit"]["subject"]["approval_capable"])

    async def test_evaluate_checks_secret_binding_policy_without_reveal(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            _seed_agent_write_intent_secret_binding(store, binding_instance="prod")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply", "product_config.apply.secret"),
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="dry_run",
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                    source_url="https://github.com/cbusillo/launchplane/issues/387",
                    reason="Preflight managed secret-backed product config.",
                    secret_bindings=["SMTP_PASSWORD"],
                    destination={
                        "kind": "runtime_environment",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                ),
            )

        response_text = json.dumps(response.json(), sort_keys=True)
        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "allowed")
        self.assertEqual(intent["secret_evidence"]["status"], "pass")
        self.assertEqual(intent["secret_evidence"]["checked_binding_keys"], ["SMTP_PASSWORD"])
        self.assertEqual(intent["secret_evidence"]["findings"], [])
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("ciphertext", response_text)

    async def test_evaluate_denies_secret_without_secret_action_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            _seed_agent_write_intent_secret_binding(store, binding_instance="prod")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply",),
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="dry_run",
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                    secret_bindings=["SMTP_PASSWORD"],
                    destination={
                        "kind": "runtime_environment",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                ),
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "authorization_denied")
        self.assertEqual(intent["secret_evidence"]["status"], "pass")

    async def test_evaluate_denies_disallowed_secret_destination(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            _seed_agent_write_intent_secret_binding(store, binding_instance="testing")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply", "product_config.apply.secret"),
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="dry_run",
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                    secret_bindings=["SMTP_PASSWORD"],
                    destination={
                        "kind": "runtime_environment",
                        "context": "sellyouroutboard",
                        "instance": "testing",
                    },
                ),
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "secret_evidence_denied")
        self.assertEqual(intent["secret_evidence"]["status"], "fail")
        self.assertEqual(
            intent["secret_evidence"]["findings"][0]["code"],
            "secret_class_not_allowed",
        )

    async def test_evaluate_requires_agent_write_intent_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_agent_write_intent_policy(
                actions=("every_code_work_request.rerun",),
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_agent_write_intent_evaluate(
            app,
            _agent_write_intent_payload(
                intent="every_code_rerun",
                mode="dry_run",
                product="launchplane",
                context="launchplane",
            ),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_openapi_includes_agent_write_intent_evaluate_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_agent_write_intent_policy(
                actions=("every_code_work_request.rerun",),
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/agent/write-intents/evaluate"]["post"]
        self.assertEqual(route["operationId"], "evaluate_agent_write_intent")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AgentWriteIntentRequest",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "409", "503"):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route["responses"][status_code]))


class FastApiProductConfigApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_config_dry_run_returns_redacted_plan_without_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _product_config_payload(),
                    idempotency_key="product-config-dry-run",
                )
            runtime_records = app_store.list_runtime_environment_records()
            secret_records = app_store.list_secret_records()
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertEqual(payload["result"]["runtime_environment"]["action"], "created")
        self.assertEqual(payload["result"]["secrets"][0]["action"], "created")
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("https://www.sellyouroutboard.com", response_text)
        self.assertEqual(runtime_records, ())
        self.assertEqual(secret_records, ())

    async def test_product_config_apply_writes_runtime_and_managed_secret(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
            )
            request_payload = {**_product_config_payload(), "mode": "apply"}

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    request_payload,
                    idempotency_key="product-config-apply",
                )
                runtime_records = app_store.list_runtime_environment_records()
                secret_records = app_store.list_secret_records()
                secret_binding = app_store.list_secret_bindings(limit=None)[0]
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(payload["result"]["runtime_environment"]["action"], "created")
        self.assertEqual(payload["result"]["secrets"][0]["action"], "created")
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("https://www.sellyouroutboard.com", response_text)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(
            runtime_records[0],
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-prod",
                instance="prod",
                env={
                    "CONTACT_EMAIL_MODE": "smtp",
                    "SELLYOUROUTBOARD_SITE_URL": "https://www.sellyouroutboard.com",
                },
                updated_at=runtime_records[0].updated_at,
                source_label="product-config-api-test",
            ),
        )
        self.assertEqual(len(secret_records), 1)
        self.assertEqual(secret_records[0].name, "SMTP_PASSWORD")
        self.assertEqual(secret_binding.binding_key, "SMTP_PASSWORD")

    async def test_product_config_apply_writes_context_secret_for_instance_safety_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                context_name="launchplane",
                instance_name="prod",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="GITHUB_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("launchplane",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(
                    action="product_config.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: app_store,
            )
            request_payload = {
                "schema_version": 1,
                "mode": "apply",
                "product": "launchplane",
                "context": "launchplane",
                "instance": "prod",
                "source_label": "issue-2018-test",
                "runtime_env": {
                    "scope": "instance",
                    "env": {"GITHUB_API_URL": "https://api.github.com"},
                },
                "secrets": [
                    {
                        "scope": "context",
                        "name": "GITHUB_TOKEN",
                        "binding_key": "GITHUB_TOKEN",
                        "value": "github-token-value",
                        "description": "Launchplane GitHub evidence token.",
                    }
                ],
            }

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    request_payload,
                    idempotency_key="context-secret-instance-safety-target",
                )
                self.assertEqual(response.status_code, 202, response.text)
                secret_record = app_store.list_secret_records()[0]
            app_store.close()

        self.assertEqual(response.json()["result"]["runtime_key_safety"]["status"], "pass")
        self.assertNotIn("github-token-value", response.text)
        self.assertEqual(secret_record.scope, "context")
        self.assertEqual(secret_record.context, "launchplane")
        self.assertEqual(secret_record.instance, "")

    async def test_product_config_apply_reports_live_target_runtime_next_action(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-prod",
                instance="prod",
                target_id="application-syo-prod",
                target_type="application",
                target_name="syo-prod-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    {**_product_config_payload(), "mode": "apply"},
                    idempotency_key="product-config-live-sync",
                )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["status"], "records_applied_live_sync_required")
        next_action = result["next_actions"][0]
        self.assertEqual(next_action["kind"], "live_target_runtime_apply")
        self.assertEqual(next_action["dry_run"]["endpoint"], "/v1/live-target-runtime/apply")
        self.assertEqual(next_action["apply"]["endpoint"], "/v1/live-target-runtime/apply")
        self.assertEqual(next_action["target"]["target_type"], "application")
        self.assertEqual(next_action["target"]["target_name"], "syo-prod-app")
        self.assertNotIn("smtp-secret-value", json.dumps(response.json(), sort_keys=True))

    async def test_product_config_human_admin_session_can_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="META_CONVERSIONS_API_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("sellyouroutboard",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=("product_config.plan", "product_config.apply")
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                dry_run_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(reason="Review Meta configuration before apply."),
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="product-config-human-dry-run",
                )
                response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        mode="apply",
                        reason="Apply reviewed Meta configuration.",
                    ),
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="product-config-human-apply",
                )
                runtime_records = app_store.list_runtime_environment_records()
                secret_records = app_store.list_secret_records()
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        response_text = json.dumps(response.json(), sort_keys=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "apply")
        self.assertNotIn("meta-conversions-api-secret-value", response_text)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(runtime_records[0].context, "sellyouroutboard")
        self.assertEqual(len(secret_records), 1)
        self.assertEqual(secret_records[0].name, "META_CONVERSIONS_API_TOKEN")

    async def test_product_config_human_admin_apply_requires_matching_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            oauth_config = _github_oauth_config()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        mode="apply",
                        reason="Apply Meta configuration without review.",
                    ),
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="product-config-human-unreviewed",
                )
            app_store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")

    async def test_product_environment_config_path_round_trip_and_replay(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            oauth_config = _github_oauth_config()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    product="example-site",
                    context="example-site",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )
            runtime_value = "https://internal.example-site.invalid"
            dry_run_payload: dict[str, object] = {
                "schema_version": 1,
                "mode": "dry-run",
                "reason": "Review the production callback URL.",
                "runtime_settings": {"INTERNAL_CALLBACK_URL": runtime_value},
            }
            dry_run_response = await _post_product_environment_config_apply(
                app,
                dry_run_payload,
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="environment-config-runtime-dry-run",
            )
            apply_payload: dict[str, object] = {
                **dry_run_payload,
                "mode": "apply",
                "reason": "Apply the reviewed production callback URL.",
                "confirmation": "APPLY example-site/prod",
            }
            apply_response = await _post_product_environment_config_apply(
                app,
                apply_payload,
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="environment-config-runtime-apply",
            )
            replay_response = await _post_product_environment_config_apply(
                app,
                apply_payload,
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="environment-config-runtime-apply",
            )
            runtime_records = app_store.list_runtime_environment_records(
                context_name="example-site",
                instance_name="prod",
            )
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"],
            apply_response.json()["trace_id"],
        )
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(runtime_records[0].env["INTERNAL_CALLBACK_URL"], runtime_value)
        self.assertNotIn(runtime_value, json.dumps(dry_run_response.json(), sort_keys=True))
        self.assertNotIn(runtime_value, json.dumps(apply_response.json(), sort_keys=True))

    async def test_product_environment_config_apply_recovers_committed_write_conflict(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    product="example-site",
                    context="example-site",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )
            runtime_value = "https://committed.example.invalid"
            dry_run_payload: dict[str, object] = {
                "mode": "dry-run",
                "reason": "Review a recoverable concurrent apply.",
                "runtime_settings": {"INTERNAL_CALLBACK_URL": runtime_value},
            }
            dry_run_response = await _post_product_environment_config_apply(
                app,
                dry_run_payload,
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="environment-config-conflict-dry-run",
            )
            apply_payload: dict[str, object] = {
                **dry_run_payload,
                "mode": "apply",
                "reason": "Apply the reviewed recoverable concurrent change.",
                "confirmation": "APPLY example-site/prod",
            }
            original_write = app_store.write_product_authority_bundle

            def commit_then_report_conflict(bundle: ProductAuthorityBundle) -> None:
                original_write(bundle)
                raise RuntimeError("simulated concurrent idempotency conflict")

            with patch.object(
                app_store,
                "write_product_authority_bundle",
                side_effect=commit_then_report_conflict,
            ):
                apply_response = await _post_product_environment_config_apply(
                    app,
                    apply_payload,
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="environment-config-conflict-apply",
                )
            runtime_records = app_store.list_runtime_environment_records(
                context_name="example-site",
                instance_name="prod",
            )
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)
        self.assertTrue(apply_response.json()["replayed"])
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(runtime_records[0].env["INTERNAL_CALLBACK_URL"], runtime_value)

    async def test_product_environment_config_secret_identity_includes_integration(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            profile_payload = _generic_site_profile_payload()
            expected_config = cast(dict[str, object], profile_payload["expected_config"])
            managed_secret_bindings = cast(
                list[dict[str, object]], expected_config["managed_secret_bindings"]
            )
            managed_secret_bindings.append(
                {
                    "binding_key": "SMTP_PASSWORD",
                    "integration": "external_service",
                    "context": "example-site",
                    "instance": "prod",
                }
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_config.plan",
                    product="example-site",
                    context="example-site",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )
            plaintext = "external-service-smtp-secret"
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_environment_config_apply(
                    app,
                    {
                        "mode": "dry-run",
                        "reason": "Review the external SMTP integration.",
                        "managed_secrets": [
                            {
                                "binding_key": "SMTP_PASSWORD",
                                "integration": "external_service",
                                "value": plaintext,
                            }
                        ],
                    },
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="environment-config-external-secret-dry-run",
                )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["secrets"][0]["integration"], "external_service")
        self.assertNotIn(plaintext, json.dumps(response.json(), sort_keys=True))

    async def test_product_environment_config_path_requires_operator_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_config_policy(action="product_config.plan"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_environment_config_apply(
            app,
            {
                "mode": "dry-run",
                "reason": "Attempt browser-owned config from automation.",
                "runtime_settings": {"INTERNAL_CALLBACK_URL": "https://example.invalid"},
            },
            idempotency_key="environment-config-actions-identity",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_environment_config_path_hides_unauthorized_existence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_config.apply",
                    product="example-site",
                    context="example-site",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )
            payload: dict[str, object] = {
                "mode": "dry-run",
                "reason": "Probe a product config target.",
                "runtime_settings": {"INTERNAL_CALLBACK_URL": "https://example.invalid"},
            }
            known_response = await _post_product_environment_config_apply(
                app,
                payload,
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
            )
            unknown_response = await _post_product_environment_config_apply(
                app,
                payload,
                product="unknown-site",
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
            )
            app_store.close()

        self.assertEqual(known_response.status_code, 404)
        self.assertEqual(unknown_response.status_code, 404)
        self.assertEqual(known_response.json()["error"]["code"], "not_found")
        self.assertEqual(
            known_response.json()["error"]["message"],
            unknown_response.json()["error"]["message"],
        )

    async def test_product_environment_config_denies_ungranted_shared_context_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_config.plan",
                    product="example-site",
                    context="example-site",
                    instances=("testing",),
                    schema_version=2,
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )

            response = await _post_product_environment_config_apply(
                app,
                {
                    "mode": "dry-run",
                    "reason": "Attempt an ungranted shared-context lane.",
                    "runtime_settings": {"INTERNAL_CALLBACK_URL": "https://example.invalid"},
                },
                environment="prod",
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
            )
            app_store.close()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_product_environment_config_path_rejects_dry_run_mismatch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    product="example-site",
                    context="example-site",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )
            await _post_product_environment_config_apply(
                app,
                {
                    "mode": "dry-run",
                    "reason": "Review the production callback URL.",
                    "runtime_settings": {"INTERNAL_CALLBACK_URL": "https://first.example.invalid"},
                },
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="environment-config-mismatch-dry-run",
            )
            response = await _post_product_environment_config_apply(
                app,
                {
                    "mode": "apply",
                    "reason": "Apply a different callback URL.",
                    "confirmation": "APPLY example-site/prod",
                    "runtime_settings": {"INTERNAL_CALLBACK_URL": "https://second.example.invalid"},
                },
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="environment-config-mismatch-apply",
            )
            app_store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")

    async def test_product_environment_config_path_never_echoes_secret_plaintext(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                context_name="example-site",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    product="example-site",
                    context="example-site",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )
            plaintext = "never-render-this-secret"
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                dry_run_response = await _post_product_environment_config_apply(
                    app,
                    {
                        "mode": "dry-run",
                        "reason": "Review SMTP credential rotation.",
                        "managed_secrets": [
                            {
                                "binding_key": "SMTP_PASSWORD",
                                "integration": "runtime_environment",
                                "value": plaintext,
                            }
                        ],
                    },
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="environment-config-secret-dry-run",
                )
                apply_response = await _post_product_environment_config_apply(
                    app,
                    {
                        "mode": "apply",
                        "reason": "Rotate the reviewed SMTP credential.",
                        "confirmation": "APPLY example-site/prod",
                        "managed_secrets": [
                            {
                                "binding_key": "SMTP_PASSWORD",
                                "integration": "runtime_environment",
                                "value": plaintext,
                            }
                        ],
                    },
                    authorization="",
                    headers=_browser_mutation_headers(session_manager, human_session),
                    idempotency_key="environment-config-secret-apply",
                )
            secret_records = app_store.list_secret_records()
            apply_idempotency_record = app_store.read_idempotency_record(
                scope=idempotency_scope(_github_human_identity()),
                route_path="/v1/products/{product}/environments/{environment}/config/apply",
                idempotency_key="environment-config-secret-apply",
            )
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)
        self.assertNotIn(plaintext, json.dumps(dry_run_response.json(), sort_keys=True))
        self.assertNotIn(plaintext, json.dumps(apply_response.json(), sort_keys=True))
        self.assertIsNotNone(apply_idempotency_record)
        assert apply_idempotency_record is not None
        self.assertTrue(apply_idempotency_record.request_fingerprint.startswith("hmac-sha256:"))
        self.assertNotIn(plaintext, apply_idempotency_record.request_fingerprint)
        self.assertEqual(len(secret_records), 1)
        self.assertEqual(secret_records[0].name, "SMTP_PASSWORD")

    async def test_product_config_apply_requires_apply_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            response = await _post_product_config_apply(
                app,
                {**_product_config_payload(), "mode": "apply"},
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(
                    action="product_config.plan",
                    context="sellyouroutboard-testing",
                ),
                record_store_factory=lambda: app_store,
            )

            response = await _post_product_config_apply(app, _product_config_payload())
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_rejects_read_only_human_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity(role="read_only"))
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_config.apply",
                    role="read_only",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )

            response = await _post_product_config_apply(
                app,
                _meta_product_config_payload(mode="apply"),
                authorization="",
                headers=_browser_mutation_headers(session_manager, human_session),
                idempotency_key="product-config-read-only-human-apply",
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_terminal_agent_remains_read_only(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_product_config_policy(action="product_config.apply"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="terminal-agent",
                terminal_agent_token_label="terminal-read-token",
            ),
        )

        response = await _post_product_config_apply(
            app,
            _meta_product_config_payload(mode="apply"),
            authorization="Bearer terminal-agent-token",
            idempotency_key="product-config-terminal-agent",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_requires_configured_local_operator_identity(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=("product_config.plan",),
                products=("sellyouroutboard",),
                contexts=("sellyouroutboard",),
                token_label="configured-write-token",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
            ),
        )

        response = await _post_product_config_apply(
            app,
            _meta_product_config_payload(reason="Dry-run Meta config from local operator."),
            authorization="Bearer local-operator-token",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_product_config_local_operator_allows_configured_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="META_CONVERSIONS_API_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("sellyouroutboard",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.plan",),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    subject="configured-local-owner",
                    token_label="configured-write-token",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="configured-local-owner",
                    local_operator_token_label="configured-write-token",
                ),
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(reason="Dry-run Meta config from local operator."),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-configured-local-operator",
                )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "dry-run")

    async def test_product_config_terminal_agent_rejects_before_payload_validation(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_product_config_policy(action="product_config.apply"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="terminal-agent",
                terminal_agent_token_label="terminal-read-token",
            ),
        )

        response = await _post_product_config_apply(
            app,
            {},
            authorization="Bearer terminal-agent-token",
            headers={"Content-Type": "application/json"},
            raw_body=b"not-json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_rejects_missing_master_key_for_secret_bundle(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(os.environ, {}, clear=True):
                response = await _post_product_config_apply(app, _product_config_payload())
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "secret_configuration_required")
        self.assertEqual(
            payload["error"]["message"],
            "Launchplane service is missing required secret write configuration.",
        )
        self.assertNotIn("LAUNCHPLANE_MASTER_ENCRYPTION_KEY", json.dumps(payload))

    async def test_product_config_requires_runtime_key_safety_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _product_config_payload(),
                    idempotency_key="product-config-missing-key-policy",
                )
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "runtime_key_safety_unavailable")
        self.assertEqual(
            payload["error"]["message"],
            "Launchplane runtime key-safety policy is unavailable.",
        )

    async def test_product_config_rejects_runtime_env_target_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            runtime_env = dict(cast(dict[str, object], request_payload["runtime_env"]))
            runtime_env["context"] = "sellyouroutboard-testing"
            request_payload["runtime_env"] = runtime_env

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("sellyouroutboard-testing", response_text)

    async def test_product_config_rejects_secret_scope_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            request_secrets = _product_config_secrets(request_payload)
            request_payload["secrets"] = [{**request_secrets[0], "scope": "global"}]

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")

    async def test_product_config_rejects_secret_target_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            request_secrets = _product_config_secrets(request_payload)
            request_payload["secrets"] = [
                {
                    **request_secrets[0],
                    "context": "sellyouroutboard-testing",
                }
            ]

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("sellyouroutboard-testing", response_text)

    async def test_product_config_local_operator_apply_requires_matching_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.apply",),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    token_label="local-owner-write",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        mode="apply",
                        reason="Apply Meta config after dry-run.",
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-apply-missing-dry-run",
                )
            app_store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")

    async def test_product_config_local_operator_apply_succeeds_after_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="META_CONVERSIONS_API_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("sellyouroutboard",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    token_label="local-owner-write",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                dry_run_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        reason="Dry-run Meta config before terminal apply."
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-dry-run",
                )
                repeat_dry_run_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        reason="Dry-run Meta config before terminal apply."
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-dry-run-repeat",
                )
                apply_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        mode="apply",
                        reason="Apply Meta config after review.",
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-apply",
                )
                runtime_records = app_store.list_runtime_environment_records()
                secret_records = app_store.list_secret_records()
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(repeat_dry_run_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(apply_response.json()["result"]["mode"], "apply")
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(len(secret_records), 1)

    async def test_product_config_matching_dry_run_uses_normalized_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    token_label="local-owner-write",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )
            value = "123456789012345"
            dry_run_payload = _meta_product_config_payload(
                reason="Review normalized runtime config."
            )
            dry_run_payload["secrets"] = []
            dry_run_payload["runtime_env"] = {"NEXT_PUBLIC_META_PIXEL_ID": value}
            dry_run_response = await _post_product_config_apply(
                app,
                dry_run_payload,
                authorization="Bearer local-operator-token",
                idempotency_key="product-config-normalized-dry-run",
            )
            apply_payload = _meta_product_config_payload(
                mode="apply",
                reason="Apply normalized runtime config.",
            )
            apply_payload["secrets"] = []
            apply_payload.pop("runtime_env")
            apply_payload["runtime_environment"] = {
                "scope": "instance",
                "context": "sellyouroutboard",
                "instance": "prod",
                "env": {"NEXT_PUBLIC_META_PIXEL_ID": value},
            }
            apply_response = await _post_product_config_apply(
                app,
                apply_payload,
                authorization="Bearer local-operator-token",
                idempotency_key="product-config-normalized-apply",
            )
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)

    async def test_product_config_dry_run_marker_accepts_concurrent_matching_insert(
        self,
    ) -> None:
        store = _ConcurrentProductConfigDryRunMarkerStore()

        with patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        ):
            store_product_config_dry_run_record(
                record_store=store,
                identity=LocalOperatorIdentity(
                    subject="local-owner-agent",
                    token_label="local-owner-write",
                ),
                request_payload=_meta_product_config_payload(
                    reason="Dry-run Meta config before terminal apply."
                ),
                trace_id="launchplane_req_product_config_dry_run",
                response=AcceptedEvidenceResponse(
                    trace_id="launchplane_req_product_config_dry_run",
                    records={},
                    result={"mode": "dry-run"},
                ),
            )

        self.assertEqual(store.read_calls, 2)
        self.assertEqual(store.write_calls, 1)

    async def test_product_config_dry_run_marker_reraises_without_concurrent_insert(
        self,
    ) -> None:
        store = _ConcurrentProductConfigDryRunMarkerStore(after_write="missing")

        with patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated duplicate dry-run marker write"):
                store_product_config_dry_run_record(
                    record_store=store,
                    identity=LocalOperatorIdentity(
                        subject="local-owner-agent",
                        token_label="local-owner-write",
                    ),
                    request_payload=_meta_product_config_payload(
                        reason="Dry-run Meta config before terminal apply."
                    ),
                    trace_id="launchplane_req_product_config_dry_run",
                    response=AcceptedEvidenceResponse(
                        trace_id="launchplane_req_product_config_dry_run",
                        records={},
                        result={"mode": "dry-run"},
                    ),
                )

        self.assertEqual(store.read_calls, 2)
        self.assertEqual(store.write_calls, 1)

    async def test_product_config_dry_run_marker_reraises_mismatched_concurrent_insert(
        self,
    ) -> None:
        store = _ConcurrentProductConfigDryRunMarkerStore(after_write="mismatched")

        with patch.dict(
            os.environ,
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated duplicate dry-run marker write"):
                store_product_config_dry_run_record(
                    record_store=store,
                    identity=LocalOperatorIdentity(
                        subject="local-owner-agent",
                        token_label="local-owner-write",
                    ),
                    request_payload=_meta_product_config_payload(
                        reason="Dry-run Meta config before terminal apply."
                    ),
                    trace_id="launchplane_req_product_config_dry_run",
                    response=AcceptedEvidenceResponse(
                        trace_id="launchplane_req_product_config_dry_run",
                        records={},
                        result={"mode": "dry-run"},
                    ),
                )

        self.assertEqual(store.read_calls, 2)
        self.assertEqual(store.write_calls, 1)

    async def test_product_config_idempotency_replay_and_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
            )
            payload = {**_product_config_payload(), "mode": "apply"}
            changed_payload = {
                **payload,
                "runtime_env": {"scope": "instance", "env": {"CONTACT_EMAIL_MODE": "api"}},
            }

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                first_response = await _post_product_config_apply(
                    app,
                    payload,
                    idempotency_key="product-config-idempotent",
                )
                replay_response = await _post_product_config_apply(
                    app,
                    payload,
                    idempotency_key="product-config-idempotent",
                )
                conflict_response = await _post_product_config_apply(
                    app,
                    changed_payload,
                    idempotency_key="product-config-idempotent",
                )
            app_store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        ProductConfigApplyResponse.model_validate(first_response.json())
        ProductConfigApplyResponse.model_validate(replay_response.json())
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"], first_response.json()["trace_id"]
        )
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_product_config_dry_run_idempotency_replay_and_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            payload = {**_product_config_payload(), "mode": "dry-run"}
            changed_payload = {
                **payload,
                "runtime_env": {"scope": "instance", "env": {"CONTACT_EMAIL_MODE": "api"}},
            }

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                first_response = await _post_product_config_apply(
                    app,
                    payload,
                    idempotency_key="product-config-dry-run-idempotent",
                )
                replay_response = await _post_product_config_apply(
                    app,
                    payload,
                    idempotency_key="product-config-dry-run-idempotent",
                )
                conflict_response = await _post_product_config_apply(
                    app,
                    changed_payload,
                    idempotency_key="product-config-dry-run-idempotent",
                )
            app_store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"], first_response.json()["trace_id"]
        )
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_product_config_accepts_legacy_flat_runtime_env(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            payload = _product_config_payload()
            payload["secrets"] = []
            payload["runtime_env"] = {"CONTACT_EMAIL_MODE": "legacy-flat"}

            response = await _post_product_config_apply(app, payload)
            app_store.close()

        self.assertEqual(response.status_code, 202)
        ProductConfigApplyResponse.model_validate(response.json())
        runtime_environment = response.json()["result"]["runtime_environment"]
        self.assertEqual(runtime_environment["keys"], ["CONTACT_EMAIL_MODE"])

    async def test_product_config_accepts_legacy_secret_identity_shorthand(self) -> None:
        for omitted_field in ("name", "binding_key"):
            with self.subTest(omitted_field=omitted_field):
                with TemporaryDirectory() as temporary_directory_name:
                    root = Path(temporary_directory_name)
                    database_url = _sqlite_database_url(root / "launchplane.sqlite3")
                    _write_runtime_key_safety_policy(database_url=database_url)
                    app_store = PostgresRecordStore(database_url=database_url)
                    app = create_launchplane_fastapi_app(
                        verifier=_StubVerifier(_identity()),
                        authz_policy=_product_config_policy(action="product_config.plan"),
                        record_store_factory=lambda: app_store,
                    )
                    payload = _product_config_payload()
                    secret = _product_config_secrets(payload)[0]
                    secret.pop(omitted_field)

                    with patch.dict(
                        os.environ,
                        {
                            control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                                "test-master-key"
                            )
                        },
                        clear=True,
                    ):
                        response = await _post_product_config_apply(app, payload)
                    app_store.close()

                self.assertEqual(response.status_code, 202)
                ProductConfigApplyResponse.model_validate(response.json())
                secret_result = response.json()["result"]["secrets"][0]
                self.assertEqual(secret_result["name"], "SMTP_PASSWORD")
                self.assertEqual(secret_result["binding_key"], "SMTP_PASSWORD")

    async def test_product_config_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: store,
            )

            response = await _post_product_config_apply(
                app,
                _product_config_payload(),
                idempotency_key="product-config-filesystem-store",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")

    async def test_product_config_validation_errors_are_sanitized(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            request_payload["secrets"] = []
            request_payload["runtime_env"] = {"scope": "instance", "env": {"API_TOKEN": "nope"}}

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("API_TOKEN", json.dumps(payload, sort_keys=True))

    async def test_openapi_includes_product_config_apply_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_config_policy(action="product_config.plan"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/product-config/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_product_config")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductConfigApplyResponse",
        )
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductConfigApplyEnvelope",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(status_code, route["responses"])
        self.assertNotIn("422", route["responses"])
        environment_route = response.json()["paths"][
            "/v1/products/{product}/environments/{environment}/config/apply"
        ]["post"]
        self.assertEqual(environment_route["operationId"], "apply_product_environment_config")
        self.assertEqual(
            environment_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductEnvironmentConfigApplyEnvelope",
        )


class FastApiProductContextCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_lane_context_repair_returns_stale_when_provider_authority_changes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")

            class _DriftingStore(PostgresRecordStore):
                def compare_and_write_product_profile_record(
                    self,
                    *,
                    expected_record: LaunchplaneProductProfileRecord,
                    replacement_record: LaunchplaneProductProfileRecord,
                    mutation: DbOnlyMutationRequest | None = None,
                    expected_provider_targets: tuple[ProviderTargetRecord, ...] = (),
                ) -> ProductProfileCompareWriteResult:
                    expected_targets = expected_provider_targets
                    target = expected_targets[1]
                    self.write_product_authority_bundle(
                        ProductAuthorityBundle(
                            provider_target_writes=(
                                ProviderTargetWrite(
                                    record=target.model_copy(
                                        update={"updated_at": "2026-08-12T12:00:00Z"}
                                    ),
                                    expected_record=target,
                                    allowed_conflicting_routes=(
                                        (
                                            expected_targets[0].context,
                                            expected_targets[0].instance,
                                        ),
                                    ),
                                ),
                            )
                        )
                    )
                    return super().compare_and_write_product_profile_record(
                        expected_record=expected_record,
                        replacement_record=replacement_record,
                        mutation=mutation,
                        expected_provider_targets=expected_provider_targets,
                    )

            app_store = _DriftingStore(database_url=database_url)
            app_store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                {
                    "product": "sellyouroutboard",
                    "display_name": "SellYourOutboard",
                    "repository": "cbusillo/sellyouroutboard",
                    "driver_id": "generic-web",
                    "image": {"repository": "ghcr.io/cbusillo/sellyouroutboard"},
                    "runtime_port": 3000,
                    "health_path": "/api/health",
                    "lanes": [
                        {
                            "instance": "testing",
                            "context": "sellyouroutboard-testing",
                        }
                    ],
                    "historical_contexts": ["sellyouroutboard-testing"],
                    "updated_at": "2026-08-05T17:01:06Z",
                    "source": "service:generic-web-onboarding",
                }
            )
            source_target = ProviderTargetRecord(
                context="sellyouroutboard-testing",
                instance="testing",
                provider_id="dokploy",
                target_category="application",
                target_id="shared-testing-target",
                display_name="sellyouroutboard-testing",
                provider_target_type="application",
                updated_at="2026-08-05T17:47:15Z",
                source_label="test",
            )
            target_target = source_target.model_copy(
                update={
                    "context": "sellyouroutboard",
                    "display_name": "syo-testing-app",
                }
            )
            app_store.write_product_profile_record(profile)
            app_store.write_provider_target_record(source_target)
            app_store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=target_target,
                            expected_absent=True,
                            allowed_conflicting_routes=(
                                (source_target.context, source_target.instance),
                            ),
                        ),
                    )
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )
            dry_run_payload: dict[str, object] = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "instance": "testing",
                "expected_current_context": "sellyouroutboard-testing",
                "requested_context": "sellyouroutboard",
                "mode": "dry-run",
                "reason": "Repair the legacy testing lane pointer.",
            }
            dry_run_response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles/lane-context-repair/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=dry_run_payload,
            )
            apply_payload = {
                **dry_run_payload,
                "mode": "apply",
                "expected_profile_sha256": product_profile_record_sha256(profile),
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }

            response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles/lane-context-repair/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "lane-context-repair-drift",
                },
                payload=apply_payload,
            )
            stored_profile = app_store.read_product_profile_record("sellyouroutboard")
            app_store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale")
        self.assertEqual(stored_profile, profile)

    async def test_lane_context_repair_applies_reviewed_single_lane_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                {
                    "product": "sellyouroutboard",
                    "display_name": "SellYourOutboard",
                    "repository": "cbusillo/sellyouroutboard",
                    "driver_id": "generic-web",
                    "image": {"repository": "ghcr.io/cbusillo/sellyouroutboard"},
                    "runtime_port": 3000,
                    "health_path": "/api/health",
                    "lanes": [
                        {
                            "instance": "testing",
                            "context": "sellyouroutboard-testing",
                        }
                    ],
                    "historical_contexts": ["sellyouroutboard-testing"],
                    "preview": {
                        "enabled": True,
                        "context": "sellyouroutboard-preview",
                    },
                    "updated_at": "2026-08-05T17:01:06Z",
                    "source": "service:generic-web-onboarding",
                }
            )
            source_target = ProviderTargetRecord(
                context="sellyouroutboard-testing",
                instance="testing",
                provider_id="dokploy",
                target_category="application",
                target_id="shared-testing-target",
                display_name="sellyouroutboard-testing",
                provider_target_type="application",
                updated_at="2026-08-05T17:47:15Z",
                source_label="test",
            )
            target_target = source_target.model_copy(
                update={
                    "context": "sellyouroutboard",
                    "display_name": "syo-testing-app",
                }
            )
            app_store.write_product_profile_record(profile)
            app_store.write_provider_target_record(source_target)
            app_store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=target_target,
                            expected_absent=True,
                            allowed_conflicting_routes=(
                                (source_target.context, source_target.instance),
                            ),
                        ),
                    )
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )
            request_payload: dict[str, object] = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "instance": "testing",
                "expected_current_context": "sellyouroutboard-testing",
                "requested_context": "sellyouroutboard",
                "mode": "dry-run",
                "reason": "Repair the legacy testing lane pointer.",
            }

            dry_run_response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles/lane-context-repair/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=request_payload,
            )
            dry_run_result = dry_run_response.json()["result"]
            apply_payload = {
                **request_payload,
                "mode": "apply",
                "expected_profile_sha256": product_profile_record_sha256(profile),
                "reviewed_plan_sha256": dry_run_result["plan_sha256"],
            }
            apply_response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles/lane-context-repair/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "lane-context-repair",
                },
                payload=apply_payload,
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/product-profiles/lane-context-repair/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "lane-context-repair",
                },
                payload=apply_payload,
            )
            stored_profile = app_store.read_product_profile_record("sellyouroutboard")
            stored_targets = app_store.list_physical_provider_target_records()
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertTrue(dry_run_result["same_physical_target"])
        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertEqual(replay_response.json()["records"], apply_response.json()["records"])
        self.assertEqual(replay_response.json()["result"], apply_response.json()["result"])
        self.assertTrue(apply_response.json()["result"]["applied"])
        self.assertEqual(stored_profile.lanes[0].context, "sellyouroutboard")
        self.assertEqual(stored_profile.preview.context, "sellyouroutboard-preview")
        self.assertEqual(stored_profile.historical_contexts, ("sellyouroutboard-testing",))
        self.assertEqual(stored_targets, (target_target, source_target))

    async def test_context_cutover_apply_updates_profile_for_authorized_workflow(self) -> None:
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
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            payload: dict[str, object] = {
                "product": "sellyouroutboard",
                "source_context": "sellyouroutboard-testing",
                "target_context": "sellyouroutboard",
                "mode": "apply",
                "display_name": "SellYourOutboard",
                "source_label": "test:context-cutover",
            }
            response = await _post_context_cutover_apply(
                app,
                payload,
                idempotency_key="profile-context-cutover",
            )
            replay_response = await _post_context_cutover_apply(
                app,
                payload,
                idempotency_key="profile-context-cutover",
            )
            stored_profile = app_store.read_product_profile_record("sellyouroutboard")
            app_store.close()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        replay_body = replay_response.json()
        self.assertEqual(body["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(replay_response.status_code, 202)
        self.assertEqual(replay_body["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(replay_body["result"], body["result"])
        self.assertEqual(body["result"]["profile"]["display_name"], "SellYourOutboard")
        self.assertEqual(stored_profile.display_name, "SellYourOutboard")
        self.assertEqual({lane.context for lane in stored_profile.lanes}, {"sellyouroutboard"})
        self.assertEqual(stored_profile.preview.context, "sellyouroutboard")

    async def test_context_cutover_apply_rejects_contexts_outside_product_boundary(
        self,
    ) -> None:
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
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _post_context_cutover_apply(
                app,
                {
                    "product": "sellyouroutboard",
                    "source_context": "verireel-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                    "display_name": "SellYourOutboard",
                },
                idempotency_key="profile-context-cutover-cross-product",
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "context_not_in_product_boundary")

    async def test_legacy_context_cleanup_apply_returns_redacted_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            profile_payload = _product_profile_payload_with_prod()
            profile_lanes = cast(tuple[dict[str, object], ...], profile_payload["lanes"])
            profile_payload["lanes"] = tuple(
                {**lane, "context": "sellyouroutboard"} for lane in profile_lanes
            )
            profile_preview = cast(dict[str, object], profile_payload["preview"])
            profile_payload["preview"] = {
                **profile_preview,
                "context": "sellyouroutboard",
            }
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
            finally:
                store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _post_legacy_context_cleanup_apply(
                app,
                {
                    "product": "sellyouroutboard",
                    "source_context": "sellyouroutboard-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                },
                idempotency_key="legacy-context-cleanup-dry-run",
            )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["records"], {"product_profile": "sellyouroutboard"})
        self.assertFalse(body["result"]["blocked"])
        self.assertEqual(body["result"]["groups"]["runtime_environment_records"], [])

    async def test_context_apply_authenticates_before_malformed_body(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(
            app,
            {},
            authorization="",
            raw_body=b"{",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_context_apply_rejects_malformed_body_after_authentication(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(
            app,
            {},
            raw_body=b"\xff",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_context_cutover_apply_reports_schema_validation_message(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(app, {"product": "sellyouroutboard"})

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(body["error"]["message"], "Request payload failed validation.")

    async def test_context_cutover_apply_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(
            app,
            {
                "product": "sellyouroutboard",
                "source_context": "sellyouroutboard-testing",
                "target_context": "sellyouroutboard",
            },
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["code"], "database_required")
        self.assertEqual(
            body["error"]["message"],
            "Product context cutover requires Launchplane database storage.",
        )

    async def test_context_cutover_apply_requires_database_before_idempotency_replay(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "sellyouroutboard",
            "source_context": "sellyouroutboard-testing",
            "target_context": "sellyouroutboard",
        }
        store = _ProductContextApplyReplayOnlyStore(
            route_path="/v1/product-profiles/context-cutover/apply",
            payload=payload,
            idempotency_key="context-cutover-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: store,
        )

        response = await _post_context_cutover_apply(
            app,
            payload,
            idempotency_key="context-cutover-replay",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")
        self.assertEqual(store.read_idempotency_calls, 0)

    async def test_legacy_context_cleanup_apply_requires_database_before_idempotency_replay(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "sellyouroutboard",
            "source_context": "sellyouroutboard-testing",
            "target_context": "sellyouroutboard",
            "mode": "dry-run",
        }
        store = _ProductContextApplyReplayOnlyStore(
            route_path="/v1/product-profiles/legacy-context-cleanup/apply",
            payload=payload,
            idempotency_key="legacy-context-cleanup-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: store,
        )

        response = await _post_legacy_context_cleanup_apply(
            app,
            payload,
            idempotency_key="legacy-context-cleanup-replay",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")
        self.assertEqual(store.read_idempotency_calls, 0)

    async def test_openapi_includes_context_apply_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        cutover_route = openapi["paths"]["/v1/product-profiles/context-cutover/apply"]["post"]
        cleanup_route = openapi["paths"]["/v1/product-profiles/legacy-context-cleanup/apply"][
            "post"
        ]
        repair_route = openapi["paths"]["/v1/product-profiles/lane-context-repair/apply"]["post"]
        self.assertEqual(cutover_route["operationId"], "apply_product_context_cutover")
        self.assertEqual(
            cleanup_route["operationId"],
            "apply_product_legacy_context_cleanup",
        )
        self.assertEqual(
            repair_route["operationId"],
            "apply_product_lane_context_repair",
        )
        self.assertEqual(
            cutover_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductContextCutoverRequest",
        )
        self.assertEqual(
            cleanup_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "LegacyContextCleanupRequest",
        )
        self.assertEqual(
            repair_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductLaneContextRepairRequest",
        )
        for route in (cutover_route, cleanup_route, repair_route):
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            for status_code in ("400", "401", "403", "404", "409", "503"):
                self.assertIn(status_code, route["responses"])

    async def test_context_cutover_audit_returns_redacted_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        payload_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(set(payload), {"status", "trace_id", "audit"})
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["audit"]["status"], "ok")
        self.assertEqual(payload["audit"]["product"], "sellyouroutboard")
        self.assertEqual(
            payload["audit"]["contexts"]["source"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_PROPERTY_ID"],
        )
        self.assertEqual(
            payload["audit"]["contexts"]["target"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_WIDGET_ID"],
        )
        self.assertIn("SMTP_PASSWORD", payload_text)
        self.assertNotIn("property-legacy", payload_text)
        self.assertNotIn("widget-canonical", payload_text)
        self.assertNotIn("smtp-password-secret", payload_text)
        self.assertNotIn("ciphertext", payload_text)
        self.assertNotIn("plaintext", payload_text)

    async def test_context_cutover_audit_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_context_cutover_audit(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_context_cutover_audit_rejects_unowned_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(
                app,
                source_context="other-site",
                target_context="sellyouroutboard",
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "context_not_in_product_boundary")

    async def test_context_cutover_audit_invalid_request_is_generic(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(
                app,
                source_context="sellyouroutboard",
                target_context="sellyouroutboard",
            )
            app_store.close()

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_context_cutover_audit_request")
        self.assertEqual(
            payload["error"]["message"],
            "Context cutover audit request is invalid.",
        )

    async def test_context_cutover_audit_rejects_unauthorized_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="verireel"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(app)
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", payload)

    async def test_context_cutover_audit_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_context_cutover_audit(app)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_product_profile_record", payload["error"]["message"])

    async def test_openapi_includes_context_cutover_audit_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/product-profiles/{product}/context-cutover-audit"]["get"]
        self.assertEqual(route["operationId"], "read_product_context_cutover_audit")
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductContextCutoverAuditResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        for status_code in ("400", "401", "403", "404", "503"):
            self.assertIn(status_code, route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["ProductContextCutoverAuditResponse"][
                "additionalProperties"
            ],
            False,
        )
