import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.product_retirement import provider_identifier_sha256
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalAdminPolicyRule,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _local_operator_bearer_config
from tests.support.auth import _StubVerifier, _identity
from tests.test_detached_application_retirement import CANDIDATE_ID, _discovery, _proof, _request


class DetachedApplicationRetirementHttpTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, store: object, *, actions: tuple[str, ...]) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_admins=(
                    LocalAdminPolicyRule(
                        subjects=("local-admin",),
                        token_labels=("local-admin-write",),
                        products=("launchplane",),
                        contexts=("launchplane",),
                        actions=actions,
                    ),
                ),
            ),
            bearer_identity_config=BearerIdentityConfig(
                local_admin_token="local-admin-token",
                local_admin_subject="local-admin",
                local_admin_token_label="local-admin-write",
            ),
            control_plane_root_path=Path("."),
            record_store_factory=lambda: store,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer local-admin-token",
            "Idempotency-Key": "retire-detached-application",
        }

    async def test_plan_is_local_admin_only_authorized_and_redacted(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=(
                    f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'launchplane.sqlite3'}"
                )
            )
            store.ensure_schema()
            denied = await _asgi_request(
                self._app(store, actions=("operations.read",)),
                "POST",
                "/v1/detached-application-retirement",
                headers=self.headers,
                payload=_request().model_dump(mode="json"),
            )
            with (
                patch(
                    "control_plane.http_app.control_plane_detached_application_retirement."
                    "discover_detached_application",
                    return_value=_discovery(),
                ),
                patch(
                    "control_plane.http_app.control_plane_detached_application_retirement."
                    "prove_detached_application_authority_absence",
                    return_value=_proof(),
                ),
            ):
                planned = await _asgi_request(
                    self._app(store, actions=("detached_application_retirement.plan",)),
                    "POST",
                    "/v1/detached-application-retirement",
                    headers=self.headers,
                    payload=_request().model_dump(mode="json"),
                )
            store.close()
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(planned.status_code, 202, planned.text)
        payload = planned.json()
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(CANDIDATE_ID, serialized)
        self.assertNotIn("application_id", serialized)
        self.assertEqual(
            payload["result"]["candidate_target_sha256"],
            provider_identifier_sha256(CANDIDATE_ID),
        )
        self.assertEqual(payload["result"]["authority_write_count"], 0)

    async def test_local_operator_is_rejected_even_if_policy_action_exists(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=(
                    f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'launchplane.sqlite3'}"
                )
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(schema_version=2),
                bearer_identity_config=_local_operator_bearer_config(),
                control_plane_root_path=Path("."),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/detached-application-retirement",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "retire-detached-application",
                },
                payload=_request().model_dump(mode="json"),
            )
            store.close()
        self.assertEqual(response.status_code, 403)

    async def test_openapi_exposes_dedicated_request_without_raw_target_id(self) -> None:
        app = self._app(object(), actions=())
        response = await _asgi_request(app, "GET", "/openapi.json")
        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/v1/detached-application-retirement"]["post"]
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("DetachedApplicationRetirementRequest", request_schema["$ref"])
        schema = response.json()["components"]["schemas"]["DetachedApplicationRetirementRequest"]
        self.assertNotIn("target_id", schema["properties"])
        self.assertIn("candidate_target_sha256", schema["properties"])


if __name__ == "__main__":
    unittest.main()
