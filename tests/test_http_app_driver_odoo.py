import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from click import ClickException

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.async_case import AsyncTestCase
from tests.http_app_test_support import (
    _asgi_get,
    _MissingProductReadStore,
    _post_odoo_artifact_publish_inputs,
)
from tests.test_service import (
    _identity,
    _invoke_app,
    _odoo_preview_profile_payload,
    _StubVerifier,
    create_launchplane_service_app,
)


class FastApiOdooArtifactPublishInputsTests(AsyncTestCase):
    def _policy(
        self,
        *,
        product: str = "odoo",
        context: str = "opw",
        repository: str = "every/tenant-opw",
        workflow_ref: str = (
            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
        ),
    ) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": repository,
                        "workflow_refs": [workflow_ref],
                        "event_names": ["workflow_dispatch"],
                        "products": [product],
                        "contexts": [context],
                        "actions": ["odoo_artifact_publish_inputs.read"],
                    }
                ]
            }
        )

    def _identity(
        self,
        *,
        repository: str = "every/tenant-opw",
        workflow_ref: str = (
            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
        ),
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _tenant_policy(self) -> LaunchplaneAuthzPolicy:
        return self._policy(
            product="odoo-tenant-cm-website",
            context="cm_website",
            repository="cbusillo/odoo-tenant-cm-website",
            workflow_ref=(
                "cbusillo/odoo-tenant-cm-website/.github/workflows/odoo-preview.yml@refs/heads/main"
            ),
        )

    def _tenant_identity(self) -> GitHubActionsIdentity:
        return self._identity(
            repository="cbusillo/odoo-tenant-cm-website",
            workflow_ref=(
                "cbusillo/odoo-tenant-cm-website/.github/workflows/odoo-preview.yml@refs/heads/main"
            ),
        )

    def _tenant_store(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload("odoo-tenant-cm-website")
        profile_payload["display_name"] = "Cell Mechanic Website Odoo"
        profile_payload["repository"] = "cbusillo/odoo-tenant-cm-website"
        profile_payload["image"] = {"repository": "ghcr.io/cbusillo/odoo-tenant-cm-website"}
        lanes = list(cast(tuple[dict[str, object], ...], profile_payload["lanes"]))
        lanes[0]["context"] = "cm_website"
        profile_payload["lanes"] = tuple(lanes)
        preview = cast(dict[str, object], profile_payload["preview"])
        preview["context"] = "cm_website"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _tenant_payload(self) -> dict[str, object]:
        return {
            "product": "odoo-tenant-cm-website",
            "inputs": {"context": "cm_website", "instance": "testing"},
        }

    async def test_odoo_artifact_publish_inputs_returns_build_scoped_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_artifact_publish_inputs_http."
                "build_odoo_artifact_publish_inputs",
                return_value={
                    "context": "opw",
                    "instance": "testing",
                    "environment": {"ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19"},
                },
            ) as build_inputs:
                response = await _post_odoo_artifact_publish_inputs(
                    app,
                    {
                        "product": "odoo",
                        "inputs": {"context": "opw", "instance": "testing"},
                    },
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {})
        self.assertEqual(
            payload["result"]["environment"],
            {"ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19"},
        )
        self.assertIsNone(build_inputs.call_args.kwargs["product_profile"])

    async def test_odoo_artifact_publish_inputs_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_artifact_publish_inputs_http."
                "build_odoo_artifact_publish_inputs",
                return_value={"context": "opw", "environment": {"A": "B"}},
            ) as build_inputs:
                first_response = await _post_odoo_artifact_publish_inputs(
                    app,
                    {
                        "product": "odoo",
                        "inputs": {"context": "opw", "instance": "testing"},
                    },
                    idempotency_key="odoo-artifact-inputs:replay",
                )
                replay_response = await _post_odoo_artifact_publish_inputs(
                    app,
                    {
                        "product": "odoo",
                        "inputs": {"context": "opw", "instance": "testing"},
                    },
                    idempotency_key="odoo-artifact-inputs:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        build_inputs.assert_called_once()

    async def test_odoo_artifact_publish_inputs_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_artifact_publish_inputs_http."
                "build_odoo_artifact_publish_inputs",
                return_value={"context": "opw", "environment": {"A": "B"}},
            ):
                first_response = await _post_odoo_artifact_publish_inputs(
                    app,
                    {
                        "product": "odoo",
                        "inputs": {"context": "opw", "instance": "testing"},
                    },
                    idempotency_key="odoo-artifact-inputs:conflict",
                )
                conflict_response = await _post_odoo_artifact_publish_inputs(
                    app,
                    {
                        "product": "odoo",
                        "inputs": {"context": "opw", "instance": "prod"},
                    },
                    idempotency_key="odoo-artifact-inputs:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_artifact_publish_inputs_rejects_unauthorized_workflow(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_odoo_artifact_publish_inputs(
            app,
            {
                "product": "odoo",
                "inputs": {"context": "opw", "instance": "testing"},
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_artifact_publish_inputs_rejects_non_odoo_product_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _odoo_preview_profile_payload("odoo-tenant-cm-website")
            profile_payload["driver_id"] = "generic-web"
            profile_payload["display_name"] = "Cell Mechanic Website Odoo"
            profile_payload["repository"] = "cbusillo/odoo-tenant-cm-website"
            profile_payload["image"] = {"repository": "ghcr.io/cbusillo/odoo-tenant-cm-website"}
            lanes = list(cast(tuple[dict[str, object], ...], profile_payload["lanes"]))
            lanes[0]["context"] = "cm_website"
            profile_payload["lanes"] = tuple(lanes)
            preview = cast(dict[str, object], profile_payload["preview"])
            preview["context"] = "cm_website"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._tenant_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_artifact_publish_inputs(app, self._tenant_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_artifact_publish_inputs_dependency_miss_is_not_route_missing(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._tenant_policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_artifact_publish_inputs(app, self._tenant_payload())

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"], "/v1/drivers/odoo/artifact-publish-inputs"
        )
        self.assertNotIn("missing", payload["details"])
        self.assertNotIn("product_profiles", json.dumps(payload))
        self.assertNotIn("No Launchplane route", payload["error"]["message"])

    async def test_odoo_artifact_publish_inputs_runtime_dependency_miss_is_dependency_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = self._tenant_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._tenant_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.workflows.odoo_artifact_publish."
                "control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={
                    "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19",
                    "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/cbusillo/devtools:19",
                },
            ) as resolve_runtime_environment_values:
                response = await _post_odoo_artifact_publish_inputs(app, self._tenant_payload())

        resolve_runtime_environment_values.assert_called_once_with(
            control_plane_root=root,
            context_name="cm_website",
            instance_name="testing",
        )
        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"], "/v1/drivers/odoo/artifact-publish-inputs"
        )
        self.assertNotIn("ODOO_DEVKIT_REPOSITORY", json.dumps(payload))
        self.assertNotIn("ODOO_SHARED_ADDONS_REPOSITORY", json.dumps(payload))

    async def test_odoo_artifact_publish_inputs_missing_runtime_records_are_dependency_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._tenant_store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._tenant_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_artifact_publish_inputs(app, self._tenant_payload())

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"], "/v1/drivers/odoo/artifact-publish-inputs"
        )
        self.assertNotIn("runtime environment", json.dumps(payload).lower())

    async def test_odoo_artifact_publish_inputs_runtime_store_error_is_not_dependency_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._tenant_store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._tenant_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.workflows.odoo_artifact_publish."
                "control_plane_runtime_environments.resolve_runtime_environment_values",
                side_effect=ClickException(
                    "Could not load runtime environments from Launchplane Postgres storage: boom"
                ),
            ):
                response = await _post_odoo_artifact_publish_inputs(app, self._tenant_payload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertNotEqual(
            response.json()["error"]["code"],
            "driver_route_dependency_not_found",
        )

    async def test_odoo_artifact_publish_inputs_handler_file_miss_is_not_dependency_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._tenant_store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._tenant_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_artifact_publish_inputs_http."
                "build_odoo_artifact_publish_inputs",
                side_effect=FileNotFoundError("handler-side file miss"),
            ) as build_publish_inputs:
                response = await _post_odoo_artifact_publish_inputs(app, self._tenant_payload())

        build_publish_inputs.assert_called_once()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertNotEqual(
            response.json()["error"]["code"],
            "driver_route_dependency_not_found",
        )

    async def test_openapi_includes_odoo_artifact_publish_inputs_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/drivers/odoo/artifact-publish-inputs"]["post"]
        self.assertEqual(route["operationId"], "write_odoo_artifact_publish_inputs")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["title"], "OdooArtifactPublishInputsEnvelope")
        self.assertIn("400", route["responses"])
        self.assertIn("401", route["responses"])
        self.assertIn("403", route["responses"])
        self.assertIn("404", route["responses"])
        self.assertIn("409", route["responses"])
        self.assertIn("503", route["responses"])

    def test_legacy_wsgi_odoo_artifact_publish_inputs_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                legacy_app,
                method="POST",
                path="/v1/drivers/odoo/artifact-publish-inputs",
                payload={
                    "product": "odoo",
                    "inputs": {"context": "opw", "instance": "testing"},
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
