from __future__ import annotations

import base64
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request
from tests.support.auth import _StubVerifier, _identity
from tests.support.stores import _sqlite_database_url


def _fernet_key(offset: int) -> str:
    return base64.urlsafe_b64encode(bytes((offset + index) % 256 for index in range(32))).decode()


def _secret_rotation_policy(*actions: str) -> LaunchplaneAuthzPolicy:
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
                    "actions": list(actions),
                }
            ]
        }
    )


class FastApiSecretRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_secret_reencryption_route_refuses_dry_run_and_apply(self) -> None:
        key1 = _fernet_key(0)
        key2 = _fernet_key(32)
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                with patch.dict(
                    os.environ,
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                            {"active_key_id": "key-1", "keys": {"key-1": key1}}
                        )
                    },
                    clear=True,
                ):
                    write_result = control_plane_secrets.write_secret_value(
                        record_store=store,
                        scope="global",
                        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
                        name="host",
                        plaintext_value="https://provider.example",
                        binding_key="DOKPLOY_HOST",
                        actor="test",
                    )

                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_secret_rotation_policy(
                        "secret.reencrypt.dry-run", "secret.reencrypt.apply"
                    ),
                    record_store_factory=lambda: store,
                )
                rotation_environment = {
                    control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                        {
                            "active_key_id": "key-2",
                            "keys": {"key-1": key1, "key-2": key2},
                        }
                    )
                }
                with patch.dict(os.environ, rotation_environment, clear=True):
                    dry_run_response = await _asgi_request(
                        app,
                        "POST",
                        "/v1/secrets/reencrypt",
                        headers={"Authorization": "Bearer valid-token"},
                        payload={
                            "schema_version": 1,
                            "mode": "dry-run",
                            "reason": "Rotate the managed-secret root.",
                            "source_label": "service-test",
                        },
                    )
                    apply_payload = {
                        "schema_version": 1,
                        "mode": "apply",
                        "expected_plan_digest": "0" * 64,
                        "reason": "Rotate the managed-secret root.",
                        "source_label": "service-test",
                    }
                    apply_headers = {
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "secret-rotation-test",
                    }
                    apply_response = await _asgi_request(
                        app,
                        "POST",
                        "/v1/secrets/reencrypt",
                        headers=apply_headers,
                        payload=apply_payload,
                    )
                secret_id = str(write_result["secret_id"])
                current_record = store.read_secret_record(secret_id)
                current_version = store.read_secret_version(current_record.current_version_id)
                versions = store.list_secret_versions(secret_id=secret_id)
            finally:
                store.close()

        self.assertEqual(dry_run_response.status_code, 409)
        self.assertEqual(
            dry_run_response.json()["error"]["code"],
            "privileged_operation_planning_required",
        )
        self.assertEqual(apply_response.status_code, 409)
        self.assertEqual(
            apply_response.json()["error"]["code"],
            "privileged_operation_approval_required",
        )
        self.assertNotIn("https://provider.example", json.dumps(apply_response.json()))
        self.assertEqual(current_version.key_id, "key-1")
        self.assertEqual(len(versions), 1)

    async def test_legacy_apply_refusal_precedes_old_apply_guards(self) -> None:
        key = _fernet_key(0)
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_secret_rotation_policy("secret.reencrypt.apply"),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "expected_plan_digest": "0" * 64,
                    "reason": "Exercise apply guards.",
                }
                with patch.dict(
                    os.environ,
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                            {"active_key_id": "key-1", "keys": {"key-1": key}}
                        )
                    },
                    clear=True,
                ):
                    missing_idempotency = await _asgi_request(
                        app,
                        "POST",
                        "/v1/secrets/reencrypt",
                        headers={"Authorization": "Bearer valid-token"},
                        payload=payload,
                    )
                    stale_plan = await _asgi_request(
                        app,
                        "POST",
                        "/v1/secrets/reencrypt",
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": "stale-secret-rotation",
                        },
                        payload=payload,
                    )
            finally:
                store.close()

        self.assertEqual(missing_idempotency.status_code, 409)
        self.assertEqual(
            missing_idempotency.json()["error"]["code"],
            "privileged_operation_approval_required",
        )
        self.assertEqual(stale_plan.status_code, 409)
        self.assertEqual(
            stale_plan.json()["error"]["code"],
            "privileged_operation_approval_required",
        )
