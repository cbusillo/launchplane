import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from click import ClickException

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_preview_runtime import OdooPreviewDokployApplyResult
from tests.http_app_test_support import (
    _asgi_get,
    _MissingProductReadStore,
    _post_odoo_artifact_publish_inputs,
    _post_odoo_preview_apply,
    _post_odoo_preview_apply_inputs,
)
from tests.test_service import (
    _identity,
    _invoke_app,
    _odoo_preview_profile_payload,
    _sqlite_database_url,
    _StubVerifier,
    _write_odoo_preview_template_runtime_environment,
    create_launchplane_service_app,
)


class FastApiOdooArtifactPublishInputsTests(unittest.IsolatedAsyncioTestCase):
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


class FastApiOdooPreviewApplyTests(unittest.IsolatedAsyncioTestCase):
    def _policy(
        self,
        *,
        actions: tuple[str, ...],
        repository: str = "cbusillo/odoo-tenant-cm",
        workflow_ref: str = (
            "cbusillo/odoo-tenant-cm/.github/workflows/odoo-preview.yml@refs/heads/main"
        ),
    ) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": repository,
                        "workflow_refs": [workflow_ref],
                        "event_names": ["workflow_dispatch"],
                        "products": ["odoo-tenant-cm"],
                        "contexts": ["cm"],
                        "actions": list(actions),
                    }
                ]
            }
        )

    def _identity(
        self,
        *,
        repository: str = "cbusillo/odoo-tenant-cm",
        workflow_ref: str = (
            "cbusillo/odoo-tenant-cm/.github/workflows/odoo-preview.yml@refs/heads/main"
        ),
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _profile_store(self, database_url: str) -> PostgresRecordStore:
        store = PostgresRecordStore(database_url=database_url)
        store.ensure_schema()
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        return store

    async def test_odoo_preview_apply_inputs_derives_runtime_and_dry_run_plans(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(database_url)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="cm",
                    env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://cm-preview.example.test"},
                    updated_at="2026-05-09T12:25:00Z",
                    source_label="test",
                )
            )
            _write_odoo_preview_template_runtime_environment(store=store)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="cm",
                    instance="testing",
                    env={
                        "ODOO_DB_USER": "odoo",
                        "DOKPLOY_ENVIRONMENT_ID": "env-cm-preview",
                    },
                    updated_at="2026-05-09T12:30:00Z",
                    source_label="test",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(actions=("odoo_preview_apply_inputs.read",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with (
                patch.dict(
                    "os.environ",
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                    return_value=DokploySourceOfTruth(
                        schema_version=1,
                        targets=(
                            DokployTargetDefinition(
                                context="cm",
                                instance="testing",
                                target_type="compose",
                                target_id="compose-cm-testing",
                                target_name="cm-testing",
                            ),
                        ),
                    ),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example", "token"),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "control_plane_dokploy.dokploy_request",
                    return_value=[{"environments": [{"composes": []}]}],
                ),
            ):
                response = await _post_odoo_preview_apply_inputs(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "inputs": {
                            "product": "odoo-tenant-cm",
                            "pr_number": 42,
                            "manifest": {
                                "artifact_id": "artifact-cm-preview",
                                "source_commit": "abc123",
                                "enterprise_base_digest": "sha256:enterprise",
                                "image": {
                                    "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                                    "digest": "sha256:abc123",
                                },
                            },
                        },
                    },
                )

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["preview_slug"], "pr-42")
        self.assertEqual(result["preview_url"], "https://pr-42.cm-preview.example.test")
        self.assertEqual(result["runtime_plan"]["status"], "ready")
        self.assertEqual(result["dry_run_plan"]["status"], "ready")
        self.assertEqual(result["dry_run_plan"]["environment_id"], "env-cm-preview")
        self.assertNotIn("template-db-secret", json.dumps(response.json()))

    async def test_odoo_preview_apply_inputs_builds_destroy_for_discovered_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(database_url)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="cm",
                    env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://cm-preview.example.test"},
                    updated_at="2026-05-09T12:25:00Z",
                    source_label="test",
                )
            )
            _write_odoo_preview_template_runtime_environment(store=store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(actions=("odoo_preview_apply_inputs.read",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            def _fake_dokploy_request(**kwargs: object) -> object:
                path = kwargs["path"]
                if path == "/api/project.all":
                    return [
                        {
                            "environments": [
                                {
                                    "composes": [
                                        {
                                            "composeId": "compose-cm-pr-42",
                                            "name": "cm-odoo-preview-pr-42",
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                if path == "/api/domain.byComposeId":
                    return [
                        {
                            "domainId": "domain-cm-pr-42",
                            "host": "pr-42.cm-preview.example.test",
                        }
                    ]
                raise AssertionError(path)

            with (
                patch.dict(
                    "os.environ",
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example", "token"),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "control_plane_dokploy.dokploy_request",
                    side_effect=_fake_dokploy_request,
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                    return_value=DokploySourceOfTruth(
                        schema_version=1,
                        targets=(
                            DokployTargetDefinition(
                                context="cm",
                                instance="testing",
                                target_type="compose",
                                target_id="compose-cm-testing",
                                target_name="cm-testing",
                            ),
                        ),
                    ),
                ),
            ):
                response = await _post_odoo_preview_apply_inputs(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "inputs": {
                            "product": "odoo-tenant-cm",
                            "operation": "destroy",
                            "pr_number": 42,
                        },
                    },
                )

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["runtime_plan"]["target"]["target_id"], "compose-cm-pr-42")
        self.assertEqual(result["dry_run_plan"]["compose_ref"], "compose-cm-pr-42")
        self.assertEqual(result["dry_run_plan"]["operation"], "destroy")

    async def test_odoo_preview_apply_inputs_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(actions=("odoo_preview_apply_inputs.read",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_preview_apply_inputs_result",
                side_effect=FileNotFoundError,
            ):
                response = await _post_odoo_preview_apply_inputs(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "inputs": {
                            "product": "odoo-tenant-cm",
                            "pr_number": 42,
                            "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                        },
                    },
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_odoo_preview_apply_inputs_ignores_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(actions=("odoo_preview_apply_inputs.read",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "inputs": {
                    "product": "odoo-tenant-cm",
                    "pr_number": 42,
                    "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                },
            }

            with patch(
                "control_plane.http_app.build_odoo_preview_apply_inputs_result",
                side_effect=(
                    {
                        "status": "blocked",
                        "product": "odoo-tenant-cm",
                        "source": "first",
                    },
                    {
                        "status": "ready",
                        "product": "odoo-tenant-cm",
                        "source": "second",
                    },
                ),
            ) as build_inputs:
                first_response = await _post_odoo_preview_apply_inputs(
                    app,
                    payload,
                    idempotency_key="odoo-preview-apply-inputs:odoo-tenant-cm:pr-42:abc123",
                )
                second_response = await _post_odoo_preview_apply_inputs(
                    app,
                    payload,
                    idempotency_key="odoo-preview-apply-inputs:odoo-tenant-cm:pr-42:abc123",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["source"], "first")
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(second_response.json()["result"]["source"], "second")
        self.assertEqual(build_inputs.call_count, 2)

    async def test_odoo_preview_apply_blocks_missing_service_runtime_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(database_url)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="cm",
                    instance="testing",
                    env={"ODOO_DB_USER": "odoo"},
                    updated_at="2026-05-09T12:30:00Z",
                    source_label="test",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("odoo_preview_apply.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply",
                return_value=OdooPreviewDokployApplyResult(
                    status="pass",
                    operation="refresh",
                    product="odoo-tenant-cm",
                    repository="cbusillo/odoo-tenant-cm",
                    preview_slug="pr-42",
                    preview_url="https://pr-42.cm-preview.example.test",
                    domain_host="pr-42.cm-preview.example.test",
                    compose_id="compose-cm-pr-42",
                    compose_name="cm-odoo-preview-pr-42",
                ),
            ) as apply_driver:
                response = await _post_odoo_preview_apply(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "apply": {
                            "dry_run_plan": {
                                "status": "ready",
                                "operation": "refresh",
                                "product": "odoo-tenant-cm",
                                "repository": "cbusillo/odoo-tenant-cm",
                                "preview_slug": "pr-42",
                                "preview_url": "https://pr-42.cm-preview.example.test",
                                "domain_host": "pr-42.cm-preview.example.test",
                                "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                                "compose_name": "cm-odoo-preview-pr-42",
                                "environment_id": "env-cm-preview",
                                "template_compose_id": "compose-cm-testing",
                                "summary": "ready isolated Odoo preview apply",
                            },
                            "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                            "wait_for_deploy": False,
                            "smoke_check": False,
                        },
                    },
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )

        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "odoo_preview_runtime_config_incomplete")
        self.assertEqual(payload["details"]["context"], "cm")
        self.assertEqual(payload["details"]["instance"], "testing")
        self.assertEqual(
            payload["details"]["missing_keys"],
            ["ODOO_ADMIN_PASSWORD", "ODOO_DB_PASSWORD", "ODOO_MASTER_PASSWORD"],
        )
        apply_driver.assert_not_called()

    async def test_odoo_preview_apply_resolves_runtime_environment_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(database_url)
            _write_odoo_preview_template_runtime_environment(store=store)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="cm",
                    instance="testing",
                    env={
                        "ODOO_DB_USER": "odoo",
                        "DOKPLOY_ENVIRONMENT_ID": "env-cm-preview",
                    },
                    updated_at="2026-05-09T12:30:00Z",
                    source_label="test",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("odoo_preview_apply.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            with (
                patch.dict(
                    "os.environ",
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply",
                    return_value=OdooPreviewDokployApplyResult(
                        status="pass",
                        operation="refresh",
                        product="odoo-tenant-cm",
                        repository="cbusillo/odoo-tenant-cm",
                        preview_slug="pr-42",
                        preview_url="https://pr-42.cm-preview.example.test",
                        domain_host="pr-42.cm-preview.example.test",
                        compose_id="compose-cm-pr-42",
                        compose_name="cm-odoo-preview-pr-42",
                        created_compose=True,
                        domain_id="domain-cm-pr-42",
                    ),
                ) as apply_driver,
            ):
                response = await _post_odoo_preview_apply(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "apply": {
                            "dry_run_plan": {
                                "status": "ready",
                                "operation": "refresh",
                                "product": "odoo-tenant-cm",
                                "repository": "cbusillo/odoo-tenant-cm",
                                "preview_slug": "pr-42",
                                "preview_url": "https://pr-42.cm-preview.example.test",
                                "domain_host": "pr-42.cm-preview.example.test",
                                "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                                "compose_name": "cm-odoo-preview-pr-42",
                                "environment_id": "env-cm-preview",
                                "template_compose_id": "compose-cm-testing",
                                "summary": "ready isolated Odoo preview apply",
                            },
                            "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                            "manifest": {
                                "artifact_id": "artifact-cm-preview",
                                "source_commit": "abc123",
                                "enterprise_base_digest": "sha256:enterprise",
                                "image": {
                                    "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                                    "digest": "sha256:abc123",
                                },
                            },
                            "environment_values": {
                                "ODOO_DB_PASSWORD": "caller-secret-must-not-win",
                            },
                            "wait_for_deploy": False,
                            "smoke_check": False,
                        },
                    },
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["status"], "pass")
        self.assertEqual(payload["result"]["compose_id"], "compose-cm-pr-42")
        self.assertNotIn("template-db-secret", json.dumps(payload))
        self.assertNotIn("caller-secret-must-not-win", json.dumps(payload))
        apply_driver.assert_called_once()
        applied_request = apply_driver.call_args.kwargs["request"]
        self.assertEqual(
            applied_request.image_reference,
            "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
        )
        self.assertEqual(applied_request.dry_run_plan.environment_id, "env-cm-preview")
        self.assertEqual(applied_request.environment_values["ODOO_DB_USER"], "odoo")
        self.assertEqual(
            applied_request.environment_values["ODOO_DB_PASSWORD"], "template-db-secret"
        )
        self.assertEqual(
            applied_request.environment_values["ODOO_MASTER_PASSWORD"],
            "template-master-secret",
        )
        self.assertEqual(
            applied_request.environment_values["ODOO_ADMIN_PASSWORD"],
            "template-admin-secret",
        )
        self.assertEqual(
            applied_request.environment_values["ODOO_DB_NAME"],
            "cm_odoo_preview_pr_42_db",
        )
        self.assertEqual(
            applied_request.environment_values["ODOO_DATA_VOLUME"],
            "cm_odoo_preview_pr_42_data",
        )
        self.assertEqual(
            applied_request.environment_values["ODOO_PROJECT_NAME"],
            "cm-odoo-preview-pr-42",
        )
        self.assertEqual(
            applied_request.environment_values["ODOO_STACK_NAME"],
            "cm-odoo-preview-pr-42",
        )
        self.assertNotEqual(
            applied_request.environment_values["ODOO_DB_PASSWORD"],
            "caller-secret-must-not-win",
        )

    async def test_odoo_preview_destroy_apply_allows_missing_image_reference(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("odoo_preview_apply.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply",
                return_value=OdooPreviewDokployApplyResult(
                    status="pass",
                    operation="destroy",
                    product="odoo-tenant-cm",
                    repository="cbusillo/odoo-tenant-cm",
                    preview_slug="pr-42",
                    preview_url="https://pr-42.cm-preview.example.test",
                    domain_host="pr-42.cm-preview.example.test",
                    compose_id="compose-cm-pr-42",
                    compose_name="cm-odoo-preview-pr-42",
                ),
            ) as apply_driver:
                response = await _post_odoo_preview_apply(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "apply": {
                            "dry_run_plan": {
                                "status": "ready",
                                "operation": "destroy",
                                "product": "odoo-tenant-cm",
                                "repository": "cbusillo/odoo-tenant-cm",
                                "preview_slug": "pr-42",
                                "preview_url": "https://pr-42.cm-preview.example.test",
                                "domain_host": "pr-42.cm-preview.example.test",
                                "compose_ref": "compose-cm-pr-42",
                                "compose_name": "cm-odoo-preview-pr-42",
                                "summary": "ready isolated Odoo preview destroy",
                            },
                            "wait_for_deploy": False,
                            "smoke_check": False,
                        },
                    },
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:destroy:abc123",
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["status"], "pass")
        apply_driver.assert_called_once()
        applied_request = apply_driver.call_args.kwargs["request"]
        self.assertEqual(applied_request.dry_run_plan.operation, "destroy")
        self.assertEqual(applied_request.image_reference, "")

    async def test_odoo_preview_apply_does_not_replay_blocked_idempotency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("odoo_preview_apply.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "apply": {
                    "dry_run_plan": {
                        "status": "ready",
                        "operation": "refresh",
                        "product": "odoo-tenant-cm",
                        "repository": "cbusillo/odoo-tenant-cm",
                        "preview_slug": "pr-42",
                        "preview_url": "https://pr-42.cm-preview.example.test",
                        "domain_host": "pr-42.cm-preview.example.test",
                        "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                        "compose_name": "cm-odoo-preview-pr-42",
                        "environment_id": "env-cm-preview",
                        "template_compose_id": "compose-cm-testing",
                        "summary": "ready isolated Odoo preview apply",
                    },
                    "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    "wait_for_deploy": False,
                    "smoke_check": False,
                },
            }

            with patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                side_effect=(
                    {
                        "status": "blocked",
                        "operation": "refresh",
                        "product": "odoo-tenant-cm",
                        "repository": "cbusillo/odoo-tenant-cm",
                        "preview_slug": "pr-42",
                        "preview_url": "https://pr-42.cm-preview.example.test",
                        "domain_host": "pr-42.cm-preview.example.test",
                        "compose_name": "cm-odoo-preview-pr-42",
                        "error_message": "provider dependency is not ready",
                    },
                    {
                        "status": "pass",
                        "operation": "refresh",
                        "product": "odoo-tenant-cm",
                        "repository": "cbusillo/odoo-tenant-cm",
                        "preview_slug": "pr-42",
                        "preview_url": "https://pr-42.cm-preview.example.test",
                        "domain_host": "pr-42.cm-preview.example.test",
                        "compose_name": "cm-odoo-preview-pr-42",
                    },
                ),
            ) as apply_driver:
                first_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )
                second_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["status"], "blocked")
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(second_response.json()["result"]["status"], "pass")
        self.assertEqual(apply_driver.call_count, 2)

    async def test_odoo_preview_apply_replays_non_blocked_idempotency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("odoo_preview_apply.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "apply": {
                    "dry_run_plan": {
                        "status": "ready",
                        "operation": "refresh",
                        "product": "odoo-tenant-cm",
                        "repository": "cbusillo/odoo-tenant-cm",
                        "preview_slug": "pr-42",
                        "preview_url": "https://pr-42.cm-preview.example.test",
                        "domain_host": "pr-42.cm-preview.example.test",
                        "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                        "compose_name": "cm-odoo-preview-pr-42",
                        "environment_id": "env-cm-preview",
                        "template_compose_id": "compose-cm-testing",
                        "summary": "ready isolated Odoo preview apply",
                    },
                    "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    "wait_for_deploy": False,
                    "smoke_check": False,
                },
            }
            changed_payload = json.loads(json.dumps(payload))
            cast(dict[str, object], changed_payload["apply"])["image_reference"] = (
                "ghcr.io/cbusillo/odoo-tenant-cm@sha256:def456"
            )

            with patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                return_value={
                    "status": "pass",
                    "operation": "refresh",
                    "product": "odoo-tenant-cm",
                    "repository": "cbusillo/odoo-tenant-cm",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.cm-preview.example.test",
                    "domain_host": "pr-42.cm-preview.example.test",
                    "compose_name": "cm-odoo-preview-pr-42",
                },
            ) as apply_driver:
                first_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )
                second_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )
                conflict_response = await _post_odoo_preview_apply(
                    app,
                    changed_payload,
                    idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["status"], "pass")
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(second_response.json()["replayed"], True)
        self.assertEqual(second_response.json()["result"]["status"], "pass")
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")
        apply_driver.assert_called_once()

    async def test_odoo_preview_apply_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("odoo_preview_apply.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                side_effect=FileNotFoundError,
            ):
                response = await _post_odoo_preview_apply(
                    app,
                    {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "apply": {
                            "dry_run_plan": {
                                "status": "ready",
                                "operation": "refresh",
                                "product": "odoo-tenant-cm",
                                "repository": "cbusillo/odoo-tenant-cm",
                                "preview_slug": "pr-42",
                                "preview_url": "https://pr-42.cm-preview.example.test",
                                "domain_host": "pr-42.cm-preview.example.test",
                                "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                                "compose_name": "cm-odoo-preview-pr-42",
                                "environment_id": "env-cm-preview",
                                "template_compose_id": "compose-cm-testing",
                                "summary": "ready isolated Odoo preview apply",
                            },
                            "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                        },
                    },
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_odoo_preview_apply_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    actions=("preview_refresh.execute",),
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml"
                        "@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_preview_apply(
                app,
                {
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "apply": {
                        "dry_run_plan": {
                            "status": "ready",
                            "operation": "refresh",
                            "product": "odoo-tenant-cm",
                            "repository": "cbusillo/odoo-tenant-cm",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.cm-preview.example.test",
                            "domain_host": "pr-42.cm-preview.example.test",
                            "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                            "compose_name": "cm-odoo-preview-pr-42",
                            "environment_id": "env-cm-preview",
                            "template_compose_id": "compose-cm-testing",
                            "summary": "ready isolated Odoo preview apply",
                        },
                        "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    },
                },
                idempotency_key="odoo-preview-apply:odoo-tenant-cm:pr-42:refresh:abc123",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_openapi_includes_odoo_preview_apply_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        inputs_route = paths["/v1/drivers/odoo/preview-apply-inputs"]["post"]
        self.assertEqual(inputs_route["operationId"], "write_odoo_preview_apply_inputs")
        self.assertEqual(
            inputs_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooPreviewApplyInputsEnvelope",
        )
        apply_route = paths["/v1/drivers/odoo/preview-apply"]["post"]
        self.assertEqual(apply_route["operationId"], "write_odoo_preview_apply")
        self.assertEqual(
            apply_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooPreviewApplyEnvelope",
        )
        for route in (inputs_route, apply_route):
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            self.assertIn("400", route["responses"])
            self.assertIn("401", route["responses"])
            self.assertIn("403", route["responses"])
            self.assertIn("404", route["responses"])
            self.assertIn("409", route["responses"])
            self.assertIn("503", route["responses"])

    def test_legacy_wsgi_odoo_preview_apply_routes_are_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
                control_plane_root_path=root,
            )

            inputs_status_code, inputs_payload = _invoke_app(
                legacy_app,
                method="POST",
                path="/v1/drivers/odoo/preview-apply-inputs",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "inputs": {"product": "odoo-tenant-cm", "pr_number": 42},
                },
            )
            apply_status_code, apply_payload = _invoke_app(
                legacy_app,
                method="POST",
                path="/v1/drivers/odoo/preview-apply",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "apply": {
                        "dry_run_plan": {
                            "status": "ready",
                            "operation": "destroy",
                            "product": "odoo-tenant-cm",
                            "repository": "cbusillo/odoo-tenant-cm",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.cm-preview.example.test",
                            "domain_host": "pr-42.cm-preview.example.test",
                            "compose_ref": "compose-cm-pr-42",
                            "compose_name": "cm-odoo-preview-pr-42",
                            "summary": "ready isolated Odoo preview destroy",
                        },
                    },
                },
            )

        self.assertEqual(inputs_status_code, 404)
        self.assertEqual(inputs_payload["error"]["code"], "not_found")
        self.assertEqual(apply_status_code, 404)
        self.assertEqual(apply_payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
