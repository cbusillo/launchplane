import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Literal, cast
from unittest.mock import patch

from click import ClickException

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimePlan
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app, idempotency_scope
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_PLAN_TTL_SECONDS,
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_artifact_publish import OdooArtifactPublishResult
from control_plane.workflows.odoo_app_maintenance import OdooAppMaintenanceResult
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewApplyInputsRequest,
    OdooPreviewApplyInputsResult,
    OdooPreviewDokployApplyResult,
)
from control_plane.workflows.odoo_prod_backup_gate import OdooProdBackupGateResult
from control_plane.workflows.odoo_prod_promotion import OdooProdPromotionResult
from control_plane.workflows.odoo_prod_promotion_inputs import OdooProdPromotionInputsResult
from control_plane.workflows.odoo_prod_promotion_run import OdooProdPromotionRunResult
from control_plane.workflows.odoo_prod_rollback import OdooProdRollbackResult
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementPlan,
)
from tests.http_app_test_support import (
    _asgi_get,
    _MissingProductReadStore,
    _post_odoo_app_maintenance,
    _post_odoo_artifact_publish,
    _post_odoo_artifact_publish_inputs,
    _post_odoo_config_parameter_override,
    _post_odoo_post_deploy,
    _post_odoo_preview_apply,
    _post_odoo_preview_apply_inputs,
    _post_odoo_prod_backup_gate,
    _post_odoo_prod_promotion,
    _post_odoo_prod_promotion_inputs,
    _post_odoo_prod_promotion_run,
    _post_odoo_prod_rollback,
    _post_odoo_stable_bootstrap,
    _post_odoo_target_replacement_apply,
    _post_odoo_target_replacement_plan,
    _post_odoo_website_bootstrap_override,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import (
    _sqlite_database_url,
    _write_odoo_preview_template_runtime_environment,
)


def _ready_odoo_preview_apply_payload(*, include_manifest: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
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
            "wait_for_deploy": True,
            "smoke_check": True,
        },
    }
    if include_manifest:
        apply = cast(dict[str, object], payload["apply"])
        apply["manifest"] = {
            "artifact_id": "artifact-cm-preview",
            "source_commit": "abc123",
            "enterprise_base_digest": "sha256:enterprise",
            "image": {
                "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                "digest": "sha256:abc123",
            },
        }
    return payload


def _ready_odoo_preview_destroy_payload() -> dict[str, object]:
    return {
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
    }


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


class FastApiOdooArtifactPublishTests(unittest.IsolatedAsyncioTestCase):
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
                        "actions": ["odoo_artifact_publish.write"],
                    }
                ]
            }
        )

    def _payload(self, *, product: str = "odoo", context: str = "opw") -> dict[str, object]:
        return {
            "product": product,
            "publish": {
                "context": context,
                "instance": "testing",
                "manifest": {
                    "artifact_id": f"artifact-{context}-new",
                    "source_commit": "2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                    "enterprise_base_digest": "sha256:enterprise",
                    "image": {
                        "repository": f"ghcr.io/cbusillo/{product}",
                        "digest": "sha256:new",
                    },
                },
            },
        }

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

    async def test_odoo_artifact_publish_writes_manifest_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_artifact_publish_http.ingest_odoo_artifact_publish_evidence",
                return_value=OdooArtifactPublishResult(
                    status="pass",
                    context="opw",
                    instance="testing",
                    artifact_id="artifact-opw-new",
                    image_repository="ghcr.io/cbusillo/odoo",
                    image_digest="sha256:new",
                    source_commit="2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                ),
            ) as ingest_evidence:
                response = await _post_odoo_artifact_publish(app, self._payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {"artifact_id": "artifact-opw-new"})
        self.assertEqual(payload["result"]["status"], "pass")
        self.assertEqual(payload["result"]["artifact_id"], "artifact-opw-new")
        ingest_evidence.assert_called_once()

    async def test_odoo_artifact_publish_accepts_product_profile_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._tenant_store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._policy(
                    product="odoo-tenant-cm-website",
                    context="cm_website",
                    repository="cbusillo/odoo-tenant-cm-website",
                    workflow_ref=(
                        "cbusillo/odoo-tenant-cm-website/.github/workflows/odoo-preview.yml@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_artifact_publish_http.ingest_odoo_artifact_publish_evidence",
                return_value=OdooArtifactPublishResult(
                    status="pass",
                    context="cm_website",
                    instance="testing",
                    artifact_id="artifact-cm_website-new",
                    image_repository="ghcr.io/cbusillo/odoo-tenant-cm-website",
                    image_digest="sha256:new",
                    source_commit="2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                ),
            ) as ingest_evidence:
                response = await _post_odoo_artifact_publish(
                    app,
                    self._payload(
                        product="odoo-tenant-cm-website",
                        context="cm_website",
                    ),
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["artifact_id"], "artifact-cm_website-new")
        ingest_evidence.assert_called_once()

    async def test_odoo_artifact_publish_rejects_product_profile_lane_mismatch(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._tenant_store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._policy(
                    product="odoo-tenant-cm-website",
                    context="different_context",
                    repository="cbusillo/odoo-tenant-cm-website",
                    workflow_ref=(
                        "cbusillo/odoo-tenant-cm-website/.github/workflows/odoo-preview.yml@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_artifact_publish(
                app,
                self._payload(
                    product="odoo-tenant-cm-website",
                    context="different_context",
                ),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_artifact_publish_missing_product_profile_is_dependency_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity()),
                authz_policy=self._policy(
                    product="odoo-tenant-cm-website",
                    context="cm_website",
                    repository="cbusillo/odoo-tenant-cm-website",
                    workflow_ref=(
                        "cbusillo/odoo-tenant-cm-website/.github/workflows/odoo-preview.yml@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_artifact_publish(
                app,
                self._payload(product="odoo-tenant-cm-website", context="cm_website"),
            )

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(payload["details"]["route_path"], "/v1/drivers/odoo/artifact-publish")

    async def test_odoo_artifact_publish_handler_file_miss_is_not_dependency_503(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        with patch(
            "control_plane.odoo_artifact_publish_http.ingest_odoo_artifact_publish_evidence",
            side_effect=FileNotFoundError("handler-side file miss"),
        ):
            response = await _post_odoo_artifact_publish(app, self._payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertNotEqual(
            response.json()["error"]["code"],
            "driver_route_dependency_not_found",
        )

    async def test_odoo_artifact_publish_rejects_malformed_payload(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_odoo_artifact_publish(app, {"product": "odoo"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_openapi_includes_odoo_artifact_publish_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/drivers/odoo/artifact-publish"]["post"]
        self.assertEqual(route["operationId"], "write_odoo_artifact_publish")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["title"], "OdooArtifactPublishEnvelope")
        self.assertIn("400", route["responses"])
        self.assertIn("401", route["responses"])
        self.assertIn("403", route["responses"])
        self.assertIn("404", route["responses"])
        self.assertIn("409", route["responses"])
        self.assertIn("503", route["responses"])


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

    def _reservation_store(self, root: Path) -> PostgresRecordStore:
        store = PostgresRecordStore(database_url=_sqlite_database_url(root / "launchplane.sqlite3"))
        store.ensure_schema()
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        return store

    def _profile_store(
        self,
        database_url: str,
        *,
        domain_certificate_type: Literal["none", "letsencrypt"] = "none",
    ) -> PostgresRecordStore:
        store = PostgresRecordStore(database_url=database_url)
        store.ensure_schema()
        profile_payload = _odoo_preview_profile_payload()
        preview_payload = cast(dict[str, object], profile_payload["preview"])
        preview_payload["domain_certificate_type"] = domain_certificate_type
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _planned_preview_result(self, payload: dict[str, object]) -> OdooPreviewApplyInputsResult:
        apply_request = OdooPreviewApplyEnvelope.model_validate(payload)
        dry_run_plan = apply_request.apply.dry_run_plan
        plan_request = OdooPreviewApplyInputsRequest(
            product=apply_request.product,
            operation=dry_run_plan.operation,
            pr_number=42,
            preview_slug=dry_run_plan.preview_slug,
            preview_url=dry_run_plan.preview_url,
            image_reference=apply_request.apply.image_reference,
            manifest=apply_request.apply.manifest,
            source_git_ref="abc123" if dry_run_plan.operation == "refresh" else "",
            source="test-issued-plan",
        )
        return OdooPreviewApplyInputsResult(
            status="ready",
            product=apply_request.product,
            context="cm",
            template_instance="testing",
            operation=dry_run_plan.operation,
            preview_slug=dry_run_plan.preview_slug,
            preview_url=dry_run_plan.preview_url,
            repository=dry_run_plan.repository,
            plan_request=plan_request,
            runtime_plan=OdooPreviewRuntimePlan(
                status="ready",
                operation=dry_run_plan.operation,
                product=apply_request.product,
                repository=dry_run_plan.repository,
                pr_number=42,
                preview_slug=dry_run_plan.preview_slug,
                preview_url=dry_run_plan.preview_url,
                strategy="isolated_dokploy_compose",
                summary="ready test Odoo preview runtime plan",
            ),
            dry_run_plan=dry_run_plan,
            source="test-issued-plan",
        )

    def _store_issued_preview_plan(
        self,
        *,
        store: PostgresRecordStore,
        identity: GitHubActionsIdentity,
        payload: dict[str, object],
        plan_id: str = "odoo-preview-plan-test",
        issued_at: datetime | None = None,
    ) -> str:
        issued_plan = issue_odoo_preview_apply_plan(
            result=self._planned_preview_result(payload),
            plan_id=plan_id,
            issued_at=issued_at,
        )
        provenance = issued_plan.plan_provenance
        assert provenance is not None
        recorded_at = provenance.issued_at.isoformat().replace("+00:00", "Z")
        store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=f"idempotency-{plan_id}",
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply-inputs",
                idempotency_key=plan_id,
                request_fingerprint="test-issued-plan-request",
                response_status_code=202,
                response_trace_id="trace-issued-plan",
                recorded_at=recorded_at,
                response_payload={
                    "status": "accepted",
                    "trace_id": "trace-issued-plan",
                    "records": {},
                    "result": issued_plan.model_dump(mode="json"),
                },
            )
        )
        return plan_id

    async def test_odoo_preview_apply_inputs_derives_runtime_and_dry_run_plans(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(
                database_url,
                domain_certificate_type="letsencrypt",
            )
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
                    "dokploy_source.read_control_plane_dokploy_source_of_truth",
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
                    "dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example", "token"),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request",
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
                    idempotency_key="odoo-preview-inputs-test-refresh",
                )

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["preview_slug"], "pr-42")
        self.assertEqual(result["preview_url"], "https://pr-42.cm-preview.example.test")
        self.assertEqual(result["runtime_plan"]["status"], "ready")
        self.assertEqual(result["dry_run_plan"]["status"], "ready")
        self.assertEqual(result["dry_run_plan"]["environment_id"], "env-cm-preview")
        self.assertEqual(result["dry_run_plan"]["domain_certificate_type"], "letsencrypt")
        self.assertEqual(result["plan_request"]["manifest"]["source_commit"], "abc123")
        self.assertTrue(result["plan_provenance"]["plan_id"].startswith("odoo-preview-plan-"))
        self.assertEqual(len(result["plan_provenance"]["plan_sha256"]), 64)
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
                    "dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example", "token"),
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request",
                    side_effect=_fake_dokploy_request,
                ),
                patch(
                    "control_plane.workflows.odoo_preview_runtime."
                    "dokploy_source.read_control_plane_dokploy_source_of_truth",
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
                    idempotency_key="odoo-preview-inputs-test-destroy",
                )

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["runtime_plan"]["target"]["target_id"], "compose-cm-pr-42")
        self.assertEqual(result["dry_run_plan"]["compose_ref"], "compose-cm-pr-42")
        self.assertEqual(result["dry_run_plan"]["operation"], "destroy")
        self.assertTrue(result["plan_provenance"]["plan_id"].startswith("odoo-preview-plan-"))

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
                    idempotency_key="odoo-preview-inputs-test-file-miss",
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_odoo_preview_apply_inputs_replays_service_issued_plan(self) -> None:
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
            planned_result = self._planned_preview_result(_ready_odoo_preview_apply_payload())

            with patch(
                "control_plane.http_app.build_odoo_preview_apply_inputs_result",
                return_value=planned_result.model_dump(mode="json"),
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
        first_result = first_response.json()["result"]
        self.assertEqual(first_result["source"], "test-issued-plan")
        self.assertTrue(first_result["plan_provenance"]["plan_id"].startswith("odoo-preview-plan-"))
        self.assertEqual(second_response.status_code, 202)
        second_payload = second_response.json()
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["result"], first_result)
        build_inputs.assert_called_once()

    async def test_odoo_preview_apply_rejects_unissued_plan_before_provider(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply_driver:
                response = await _post_odoo_preview_apply(
                    app,
                    _ready_odoo_preview_apply_payload(),
                    idempotency_key="odoo-preview-plan-not-issued",
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "odoo_preview_plan_not_issued")
        apply_driver.assert_not_called()

    async def test_odoo_preview_apply_rejects_tampered_plan_before_provider(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            issued_payload = _ready_odoo_preview_apply_payload()
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=issued_payload,
            )
            tampered_payload = json.loads(json.dumps(issued_payload))
            tampered_plan = cast(
                dict[str, object],
                cast(dict[str, object], tampered_payload["apply"])["dry_run_plan"],
            )
            tampered_plan["compose_name"] = "forged-compose"

            with patch("control_plane.http_app.execute_odoo_preview_apply_result") as apply_driver:
                response = await _post_odoo_preview_apply(
                    app,
                    tampered_payload,
                    idempotency_key=plan_id,
                )
            stored_apply = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply",
                idempotency_key=plan_id,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "odoo_preview_plan_mismatch")
        self.assertIsNone(stored_apply)
        apply_driver.assert_not_called()

    async def test_odoo_preview_apply_rejects_expired_plan_before_provider_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = _ready_odoo_preview_apply_payload()
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=payload,
                issued_at=datetime.now(timezone.utc)
                - timedelta(seconds=ODOO_PREVIEW_PLAN_TTL_SECONDS + 1),
            )

            with patch(
                "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply"
            ) as apply_driver:
                response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key=plan_id,
                )
            stored_apply = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply",
                idempotency_key=plan_id,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "odoo_preview_plan_expired")
        self.assertIsNone(stored_apply)
        apply_driver.assert_not_called()

    async def test_odoo_preview_apply_replays_completed_result_after_plan_expiry(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = _ready_odoo_preview_apply_payload()
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=payload,
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
                    idempotency_key=plan_id,
                )
                plan_record = store.read_idempotency_record(
                    scope=idempotency_scope(identity),
                    route_path="/v1/drivers/odoo/preview-apply-inputs",
                    idempotency_key=plan_id,
                )
                assert plan_record is not None
                issued_plan = OdooPreviewApplyInputsResult.model_validate(
                    plan_record.response_payload["result"]
                )
                provenance = issued_plan.plan_provenance
                assert provenance is not None
                expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                expired_plan = issued_plan.model_copy(
                    update={
                        "plan_provenance": provenance.model_copy(
                            update={
                                "issued_at": expired_at
                                - timedelta(seconds=ODOO_PREVIEW_PLAN_TTL_SECONDS),
                                "expires_at": expired_at,
                            }
                        )
                    }
                )
                expired_response_payload = dict(plan_record.response_payload)
                expired_response_payload["result"] = expired_plan.model_dump(mode="json")
                store.write_idempotency_record(
                    plan_record.model_copy(update={"response_payload": expired_response_payload})
                )
                replay_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key=plan_id,
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        apply_driver.assert_called_once()

    async def test_odoo_preview_apply_rejects_stale_recomputed_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=self._policy(actions=("odoo_preview_apply.execute",)),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            payload = _ready_odoo_preview_apply_payload()
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=payload,
            )
            current_plan = self._planned_preview_result(payload)
            current_plan = current_plan.model_copy(
                update={
                    "dry_run_plan": current_plan.dry_run_plan.model_copy(
                        update={"environment_id": "env-changed"}
                    )
                }
            )

            with (
                patch(
                    "control_plane.odoo_preview_apply_http.build_odoo_preview_apply_inputs",
                    return_value=current_plan,
                ),
                patch(
                    "control_plane.odoo_preview_apply_http.execute_odoo_preview_dokploy_apply"
                ) as apply_driver,
            ):
                response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key=plan_id,
                )
            stored_apply = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply",
                idempotency_key=plan_id,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "odoo_preview_plan_stale")
        self.assertIsNone(stored_apply)
        apply_driver.assert_not_called()

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
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_apply_payload(),
            )

            with (
                patch(
                    "control_plane.odoo_preview_apply_http.refresh_odoo_preview_issued_plan",
                    side_effect=lambda **kwargs: kwargs["request"],
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
                            "wait_for_deploy": False,
                            "smoke_check": False,
                        },
                    },
                    idempotency_key=plan_id,
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
            store = self._profile_store(
                database_url,
                domain_certificate_type="letsencrypt",
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
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_apply_payload(include_manifest=True),
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
                    "control_plane.odoo_preview_apply_http.refresh_odoo_preview_issued_plan",
                    side_effect=lambda **kwargs: kwargs["request"],
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
                    idempotency_key=plan_id,
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
        self.assertEqual(
            applied_request.dry_run_plan.domain_certificate_type,
            "letsencrypt",
        )
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

    async def test_odoo_preview_apply_keeps_health_responsive_during_provider_wait(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_apply_payload(),
            )
            apply_started = Event()
            release_apply = Event()
            apply_wait_timed_out = Event()

            def blocking_apply(**_: object) -> dict[str, object]:
                apply_started.set()
                if not release_apply.wait(timeout=5):
                    apply_wait_timed_out.set()
                return {
                    "status": "pass",
                    "operation": "refresh",
                    "product": "odoo-tenant-cm",
                    "repository": "cbusillo/odoo-tenant-cm",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.cm-preview.example.test",
                    "domain_host": "pr-42.cm-preview.example.test",
                    "compose_name": "cm-odoo-preview-pr-42",
                }

            with patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                side_effect=blocking_apply,
            ):
                apply_task = asyncio.create_task(
                    _post_odoo_preview_apply(
                        app,
                        _ready_odoo_preview_apply_payload(),
                        idempotency_key=plan_id,
                    )
                )
                try:
                    self.assertTrue(await asyncio.to_thread(apply_started.wait, 5))
                    health_response = await asyncio.wait_for(
                        _asgi_get(app, "/v1/health"), timeout=5
                    )
                    self.assertEqual(health_response.status_code, 200)
                    self.assertFalse(apply_wait_timed_out.is_set())
                    self.assertFalse(apply_task.done())
                finally:
                    release_apply.set()
                    apply_response = await asyncio.wait_for(apply_task, timeout=5)

        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(apply_response.json()["result"]["status"], "pass")

    async def test_odoo_preview_apply_records_result_after_caller_cancellation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_apply_payload(),
            )
            apply_started = Event()
            release_apply = Event()
            apply_wait_timed_out = Event()

            def blocking_apply(**_: object) -> dict[str, object]:
                apply_started.set()
                if not release_apply.wait(timeout=5):
                    apply_wait_timed_out.set()
                return {
                    "status": "pass",
                    "operation": "refresh",
                    "product": "odoo-tenant-cm",
                    "repository": "cbusillo/odoo-tenant-cm",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.cm-preview.example.test",
                    "domain_host": "pr-42.cm-preview.example.test",
                    "compose_name": "cm-odoo-preview-pr-42",
                }

            idempotency_key = plan_id
            with patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                side_effect=blocking_apply,
            ) as apply_driver:
                first_apply_task = asyncio.create_task(
                    _post_odoo_preview_apply(
                        app,
                        _ready_odoo_preview_apply_payload(),
                        idempotency_key=idempotency_key,
                    )
                )
                self.assertTrue(await asyncio.to_thread(apply_started.wait, 5))
                first_apply_task.cancel()
                await asyncio.sleep(0.1)
                self.assertFalse(first_apply_task.done())
                first_apply_task.cancel()
                in_progress_response = await _post_odoo_preview_apply(
                    app,
                    _ready_odoo_preview_apply_payload(),
                    idempotency_key=idempotency_key,
                )
                self.assertEqual(in_progress_response.status_code, 409)
                self.assertEqual(
                    in_progress_response.json()["error"]["code"],
                    "mutation_in_progress",
                )
                self.assertEqual(apply_driver.call_count, 1)
                release_apply.set()

                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first_apply_task, timeout=5)
                replay_response = await _post_odoo_preview_apply(
                    app,
                    _ready_odoo_preview_apply_payload(),
                    idempotency_key=idempotency_key,
                )

        self.assertFalse(apply_wait_timed_out.is_set())
        self.assertEqual(apply_driver.call_count, 1)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(replay_response.json()["result"]["status"], "pass")

    async def test_odoo_preview_post_dispatch_failure_requires_reconciliation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            failed_result = {
                "status": "fail",
                "operation": "refresh",
                "product": "odoo-tenant-cm",
                "repository": "cbusillo/odoo-tenant-cm",
                "preview_slug": "pr-42",
                "preview_url": "https://pr-42.cm-preview.example.test",
                "domain_host": "pr-42.cm-preview.example.test",
                "compose_name": "cm-odoo-preview-pr-42",
                "provider_effect_attempted": True,
                "error_message": "Timed out waiting for Dokploy deployment status.",
            }
            first_key = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_apply_payload(),
                plan_id="odoo-preview-plan-timeout",
            )
            with patch(
                "control_plane.http_app.execute_odoo_preview_apply_result",
                return_value=failed_result,
            ) as apply_driver:
                first_response = await _post_odoo_preview_apply(
                    app,
                    _ready_odoo_preview_apply_payload(),
                    idempotency_key=first_key,
                )
                second_payload = _ready_odoo_preview_apply_payload()
                second_plan = cast(
                    dict[str, object],
                    cast(dict[str, object], second_payload["apply"])["dry_run_plan"],
                )
                second_plan["compose_ref"] = "compose-cm-pr-42-existing"
                second_plan["environment_id"] = ""
                second_key = self._store_issued_preview_plan(
                    store=store,
                    identity=identity,
                    payload=second_payload,
                    plan_id="odoo-preview-plan-second",
                )
                second_response = await _post_odoo_preview_apply(
                    app,
                    second_payload,
                    idempotency_key=second_key,
                )

            reconcile_stored = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply",
                idempotency_key=first_key,
            )
            observed_failure = dict(failed_result)
            observed_failure.pop("provider_effect_attempted", None)
            with patch(
                "control_plane.http_app.observe_odoo_preview_apply_result",
                return_value=("present", observed_failure, False),
            ) as observe_driver:
                recovered_response = await _post_odoo_preview_apply(
                    app,
                    _ready_odoo_preview_apply_payload(),
                    idempotency_key=first_key,
                )
                replayed_failure_response = await _post_odoo_preview_apply(
                    app,
                    _ready_odoo_preview_apply_payload(),
                    idempotency_key=first_key,
                )
            stored = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path="/v1/drivers/odoo/preview-apply",
                idempotency_key=first_key,
            )

        self.assertEqual(first_response.status_code, 409)
        self.assertEqual(
            first_response.json()["error"]["code"],
            "mutation_reconciliation_required",
        )
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(second_response.json()["error"]["code"], "mutation_in_progress")
        self.assertEqual(apply_driver.call_count, 1)
        assert reconcile_stored is not None
        self.assertEqual(reconcile_stored.state, "reconcile_required")
        self.assertEqual(recovered_response.status_code, 502)
        self.assertEqual(
            recovered_response.json()["error"]["code"],
            "provider_mutation_failed",
        )
        self.assertEqual(replayed_failure_response.status_code, 502)
        self.assertEqual(observe_driver.call_count, 1)
        assert stored is not None
        self.assertEqual(stored.state, "completed")
        self.assertEqual(stored.response_status_code, 502)

    async def test_odoo_preview_destroy_apply_allows_missing_image_reference(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = self._profile_store(database_url)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_destroy_payload(),
            )

            with (
                patch(
                    "control_plane.odoo_preview_apply_http.refresh_odoo_preview_issued_plan",
                    side_effect=lambda **kwargs: kwargs["request"],
                ),
                patch(
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
                    idempotency_key=plan_id,
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
            store = self._reservation_store(root)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=payload,
            )

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
                    idempotency_key=plan_id,
                )
                second_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key=plan_id,
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["status"], "blocked")
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(second_response.json()["result"]["status"], "pass")
        self.assertEqual(apply_driver.call_count, 2)

    async def test_odoo_preview_apply_replays_non_blocked_idempotency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=payload,
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
                    idempotency_key=plan_id,
                )
                second_response = await _post_odoo_preview_apply(
                    app,
                    payload,
                    idempotency_key=plan_id,
                )
                conflict_response = await _post_odoo_preview_apply(
                    app,
                    changed_payload,
                    idempotency_key=plan_id,
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["status"], "pass")
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(second_response.json()["replayed"], True)
        self.assertEqual(second_response.json()["result"]["status"], "pass")
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "odoo_preview_plan_mismatch")
        apply_driver.assert_called_once()

    async def test_odoo_preview_apply_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._reservation_store(root)
            identity = self._identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/odoo-preview-apply.yml@refs/heads/main"
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
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
            plan_id = self._store_issued_preview_plan(
                store=store,
                identity=identity,
                payload=_ready_odoo_preview_apply_payload(),
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
                    idempotency_key=plan_id,
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


class FastApiOdooProdPromotionTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "every/tenant-cm",
        workflow_ref: str = (
            "every/tenant-cm/.github/workflows/odoo-prod-promotion.yml@refs/heads/main"
        ),
        job_workflow_ref: str = "",
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            job_workflow_ref=job_workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo",
        context: str = "cm",
        action: str = "odoo_prod_promotion_inputs.read",
        repository: str = "every/tenant-cm",
        workflow_ref: str = (
            "every/tenant-cm/.github/workflows/odoo-prod-promotion.yml@refs/heads/main"
        ),
        job_workflow_ref: str = "",
    ) -> LaunchplaneAuthzPolicy:
        policy_entry: dict[str, object] = {
            "repository": repository,
            "workflow_refs": [workflow_ref],
            "event_names": ["workflow_dispatch"],
            "products": [product],
            "contexts": [context],
            "actions": [action],
        }
        if job_workflow_ref:
            policy_entry["job_workflow_refs"] = [job_workflow_ref]
        return LaunchplaneAuthzPolicy.model_validate({"github_actions": [policy_entry]})

    def _store_with_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        return store

    def _store_with_non_odoo_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _inputs_payload(self, *, product: str = "odoo") -> dict[str, object]:
        return {
            "product": product,
            "inputs": {
                "context": "cm",
                "from_instance": "testing",
                "to_instance": "prod",
                "request_id": "run-123-attempt-1",
            },
        }

    def _run_payload(self, *, product: str = "odoo") -> dict[str, object]:
        return {
            "product": product,
            "run": {
                "context": "cm",
                "request_id": "run-123-attempt-1",
            },
        }

    def _promotion_payload(self, *, product: str = "odoo") -> dict[str, object]:
        return {
            "product": product,
            "promotion": {
                "context": "cm",
                "from_instance": "testing",
                "to_instance": "prod",
                "artifact_id": "artifact-cm-new",
                "backup_record_id": "backup-gate-cm-prod-run-1",
                "source_git_ref": "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
            },
        }

    def _ready_inputs_result(
        self, *, artifact_id: str = "artifact-cm-new"
    ) -> OdooProdPromotionInputsResult:
        return OdooProdPromotionInputsResult(
            context="cm",
            from_instance="testing",
            to_instance="prod",
            request_id="run-123-attempt-1",
            input_status="ready",
            artifact_id=artifact_id,
            source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
            backup_record_id="backup-gate-cm-prod-run-123-attempt-1",
            release_tuple_id="cm-testing-artifact-cm-new",
            image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
            image_digest="sha256:new",
        )

    def _blocked_inputs_result(
        self, *, error_message: str = "missing tuple"
    ) -> OdooProdPromotionInputsResult:
        return OdooProdPromotionInputsResult(
            context="cm",
            from_instance="testing",
            to_instance="prod",
            request_id="run-123-attempt-1",
            input_status="blocked",
            error_message=error_message,
        )

    def _run_result(
        self,
        *,
        run_status: Literal["pass", "fail", "blocked"] = "pass",
    ) -> OdooProdPromotionRunResult:
        return OdooProdPromotionRunResult(
            context="cm",
            from_instance="testing",
            to_instance="prod",
            request_id="run-123-attempt-1",
            run_status=run_status,
            input_status="ready" if run_status != "blocked" else "blocked",
            backup_status="pass" if run_status == "pass" else "skipped",
            promotion_status="pass" if run_status == "pass" else "skipped",
            deployment_status="pass" if run_status == "pass" else "skipped",
            post_deploy_status="pass" if run_status == "pass" else "skipped",
            destination_health_status="pass" if run_status == "pass" else "skipped",
            artifact_id="artifact-cm-new",
            source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
            backup_record_id="backup-gate-cm-prod-run-123-attempt-1",
            promotion_record_id="promotion-cm-testing-to-prod",
            deployment_record_id="deployment-cm-prod",
            release_tuple_id="cm-prod-artifact-cm-new",
            image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
            image_digest="sha256:new",
            error_message="blocked" if run_status == "blocked" else "",
        )

    def _promotion_result(
        self,
        *,
        promotion_status: Literal["pass", "fail"] = "pass",
    ) -> OdooProdPromotionResult:
        return OdooProdPromotionResult(
            context="cm",
            from_instance="testing",
            to_instance="prod",
            artifact_id="artifact-cm-new",
            backup_record_id="backup-gate-cm-prod-run-1",
            promotion_record_id="promotion-cm-testing-to-prod",
            deployment_record_id="deployment-cm-prod",
            release_tuple_id="cm-prod-artifact-cm-new",
            promotion_status=promotion_status,
            deployment_status="pass" if promotion_status == "pass" else "skipped",
            post_deploy_status="pass" if promotion_status == "pass" else "skipped",
            destination_health_status="pass" if promotion_status == "pass" else "skipped",
            error_message="failed" if promotion_status == "fail" else "",
        )

    async def test_odoo_prod_promotion_executes_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
                return_value=self._promotion_result(),
            ) as execute_mock:
                response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(),
                    idempotency_key="odoo-prod-promotion-cm",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {
                "promotion_record_id": "promotion-cm-testing-to-prod",
                "deployment_record_id": "deployment-cm-prod",
                "backup_record_id": "backup-gate-cm-prod-run-1",
                "release_tuple_id": "cm-prod-artifact-cm-new",
            },
        )
        self.assertEqual(payload["result"]["promotion_status"], "pass")
        self.assertEqual(payload["result"]["destination_health_status"], "pass")
        execute_mock.assert_called_once()
        promotion_call = execute_mock.call_args.kwargs
        self.assertEqual(promotion_call["control_plane_root"], root)
        self.assertEqual(promotion_call["state_dir"], root / "state")
        self.assertIsNone(promotion_call["database_url"])
        self.assertEqual(promotion_call["request"].context, "cm")
        self.assertEqual(promotion_call["request"].from_instance, "testing")
        self.assertEqual(promotion_call["request"].to_instance, "prod")
        self.assertEqual(promotion_call["request"].product, "odoo")

    async def test_odoo_prod_promotion_accepts_product_profile_driver_id(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/"
                            "odoo-prod-promotion.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=self._policy(
                    product="odoo-tenant-cm",
                    action="odoo_prod_promotion.execute",
                    repository="cbusillo/odoo-tenant-cm",
                    workflow_ref=(
                        "cbusillo/odoo-tenant-cm/.github/workflows/"
                        "odoo-prod-promotion.yml@refs/heads/main"
                    ),
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
                return_value=self._promotion_result(),
            ) as execute_mock:
                response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(product="odoo-tenant-cm"),
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["promotion_status"], "pass")
        promotion_call = execute_mock.call_args.kwargs
        self.assertEqual(promotion_call["request"].product, "odoo-tenant-cm")

    async def test_odoo_prod_promotion_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
                return_value=self._promotion_result(),
            ) as execute_mock:
                first_response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(),
                    idempotency_key="odoo-prod-promotion:replay",
                )
                replay_response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(),
                    idempotency_key="odoo-prod-promotion:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        execute_mock.assert_called_once()

    async def test_odoo_prod_promotion_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
                return_value=self._promotion_result(),
            ):
                first_response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(),
                    idempotency_key="odoo-prod-promotion:conflict",
                )
                conflict_response = await _post_odoo_prod_promotion(
                    app,
                    {
                        "product": "odoo",
                        "promotion": {
                            "context": "cm",
                            "from_instance": "testing",
                            "to_instance": "prod",
                            "artifact_id": "artifact-cm-other",
                            "backup_record_id": "backup-gate-cm-prod-run-1",
                        },
                    },
                    idempotency_key="odoo-prod-promotion:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_prod_promotion_does_not_replay_failed_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
                side_effect=(
                    self._promotion_result(promotion_status="fail"),
                    self._promotion_result(),
                ),
            ) as execute_mock:
                first_response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(),
                    idempotency_key="odoo-prod-promotion:failed",
                )
                second_response = await _post_odoo_prod_promotion(
                    app,
                    self._promotion_payload(),
                    idempotency_key="odoo-prod-promotion:failed",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["promotion_status"], "fail")
        self.assertEqual(second_response.status_code, 202)
        self.assertNotIn("replayed", second_response.json())
        self.assertEqual(second_response.json()["result"]["promotion_status"], "pass")
        self.assertEqual(execute_mock.call_count, 2)

    async def test_odoo_prod_promotion_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_backup_gate.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion"
            ) as execute_mock:
                response = await _post_odoo_prod_promotion(app, self._promotion_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        execute_mock.assert_not_called()

    async def test_odoo_prod_promotion_rejects_non_odoo_product_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            response = await _post_odoo_prod_promotion(
                app,
                self._promotion_payload(product="odoo-tenant-cm"),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_prod_promotion_dependency_miss_is_dependency_503(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(
                    product="odoo-tenant-cm", action="odoo_prod_promotion.execute"
                ),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            response = await _post_odoo_prod_promotion(
                app,
                self._promotion_payload(product="odoo-tenant-cm"),
            )

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(payload["details"]["route_path"], "/v1/drivers/odoo/prod-promotion")

    async def test_odoo_prod_promotion_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion",
                side_effect=FileNotFoundError("missing manifest"),
            ):
                response = await _post_odoo_prod_promotion(app, self._promotion_payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_odoo_prod_promotion_inputs_resolves_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.resolve_odoo_prod_promotion_inputs",
                return_value=self._ready_inputs_result(),
            ) as resolve_mock:
                response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(),
                    idempotency_key="odoo-prod-promotion-inputs-cm",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(
            payload["records"],
            {
                "artifact_id": "artifact-cm-new",
                "backup_record_id": "backup-gate-cm-prod-run-123-attempt-1",
                "release_tuple_id": "cm-testing-artifact-cm-new",
            },
        )
        self.assertEqual(payload["result"]["input_status"], "ready")
        self.assertEqual(payload["result"]["image_digest"], "sha256:new")
        resolve_mock.assert_called_once()

    async def test_odoo_prod_promotion_inputs_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.resolve_odoo_prod_promotion_inputs",
                return_value=self._ready_inputs_result(),
            ) as resolve_mock:
                first_response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(),
                    idempotency_key="odoo-prod-promotion-inputs:replay",
                )
                replay_response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(),
                    idempotency_key="odoo-prod-promotion-inputs:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        resolve_mock.assert_called_once()

    async def test_odoo_prod_promotion_inputs_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.resolve_odoo_prod_promotion_inputs",
                return_value=self._ready_inputs_result(),
            ):
                first_response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(),
                    idempotency_key="odoo-prod-promotion-inputs:conflict",
                )
                conflict_response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(product="odoo")
                    | {"inputs": {"context": "cm", "request_id": "run-123-attempt-2"}},
                    idempotency_key="odoo-prod-promotion-inputs:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_prod_promotion_inputs_does_not_replay_blocked_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.resolve_odoo_prod_promotion_inputs",
                side_effect=(
                    self._blocked_inputs_result(error_message="first block"),
                    self._blocked_inputs_result(error_message="second block"),
                ),
            ) as resolve_mock:
                first_response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(),
                    idempotency_key="odoo-prod-promotion-inputs:blocked",
                )
                second_response = await _post_odoo_prod_promotion_inputs(
                    app,
                    self._inputs_payload(),
                    idempotency_key="odoo-prod-promotion-inputs:blocked",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertNotIn("replayed", second_response.json())
        self.assertEqual(second_response.json()["result"]["error_message"], "second block")
        self.assertEqual(resolve_mock.call_count, 2)

    async def test_odoo_prod_promotion_inputs_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            response = await _post_odoo_prod_promotion_inputs(app, self._inputs_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_prod_promotion_inputs_dependency_miss_is_dependency_503(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            response = await _post_odoo_prod_promotion_inputs(
                app,
                self._inputs_payload(product="odoo-tenant-cm"),
                idempotency_key="odoo-prod-promotion-inputs:dependency",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(payload["details"]["route_path"], "/v1/drivers/odoo/prod-promotion-inputs")

    async def test_odoo_prod_promotion_inputs_rejects_non_odoo_product_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            response = await _post_odoo_prod_promotion_inputs(
                app,
                self._inputs_payload(product="odoo-tenant-cm"),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_prod_promotion_run_executes_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion_run.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
                return_value=self._run_result(),
            ) as execute_mock:
                response = await _post_odoo_prod_promotion_run(
                    app,
                    self._run_payload(),
                    idempotency_key="odoo-prod-promotion-run-cm",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["run_status"], "pass")
        self.assertEqual(
            payload["records"],
            {
                "artifact_id": "artifact-cm-new",
                "backup_record_id": "backup-gate-cm-prod-run-123-attempt-1",
                "promotion_record_id": "promotion-cm-testing-to-prod",
                "deployment_record_id": "deployment-cm-prod",
                "release_tuple_id": "cm-prod-artifact-cm-new",
                "request_id": "run-123-attempt-1",
            },
        )
        run_call = execute_mock.call_args.kwargs
        self.assertEqual(run_call["control_plane_root"], root)
        self.assertEqual(run_call["state_dir"], root / "state")
        self.assertIsNone(run_call["database_url"])
        self.assertEqual(run_call["request"].product, "odoo")

    async def test_odoo_prod_promotion_run_allows_reusable_launchplane_workflow(self) -> None:
        reusable_ref = "cbusillo/launchplane/.github/workflows/reusable-product-driver-prod-promotion.yml@refs/heads/main"
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity(job_workflow_ref=reusable_ref)),
                authz_policy=self._policy(
                    action="odoo_prod_promotion_run.execute",
                    job_workflow_ref=reusable_ref,
                ),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
                return_value=self._run_result(),
            ):
                response = await _post_odoo_prod_promotion_run(app, self._run_payload())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["run_status"], "pass")

    async def test_odoo_prod_promotion_run_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion_run.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
                return_value=self._run_result(),
            ) as execute_mock:
                first_response = await _post_odoo_prod_promotion_run(
                    app,
                    self._run_payload(),
                    idempotency_key="odoo-prod-promotion-run:replay",
                )
                replay_response = await _post_odoo_prod_promotion_run(
                    app,
                    self._run_payload(),
                    idempotency_key="odoo-prod-promotion-run:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        execute_mock.assert_called_once()

    async def test_odoo_prod_promotion_run_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion_run.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
                return_value=self._run_result(),
            ):
                first_response = await _post_odoo_prod_promotion_run(
                    app,
                    self._run_payload(),
                    idempotency_key="odoo-prod-promotion-run:conflict",
                )
                conflict_response = await _post_odoo_prod_promotion_run(
                    app,
                    {
                        "product": "odoo",
                        "run": {"context": "cm", "request_id": "run-123-attempt-2"},
                    },
                    idempotency_key="odoo-prod-promotion-run:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_prod_promotion_run_does_not_replay_blocked_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion_run.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
                side_effect=(self._run_result(run_status="blocked"), self._run_result()),
            ) as execute_mock:
                first_response = await _post_odoo_prod_promotion_run(
                    app,
                    self._run_payload(),
                    idempotency_key="odoo-prod-promotion-run:blocked",
                )
                second_response = await _post_odoo_prod_promotion_run(
                    app,
                    self._run_payload(),
                    idempotency_key="odoo-prod-promotion-run:blocked",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["run_status"], "blocked")
        self.assertEqual(second_response.status_code, 202)
        self.assertNotIn("replayed", second_response.json())
        self.assertEqual(second_response.json()["result"]["run_status"], "pass")
        self.assertEqual(execute_mock.call_count, 2)

    async def test_odoo_prod_promotion_run_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run"
            ) as execute_mock:
                response = await _post_odoo_prod_promotion_run(app, self._run_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        execute_mock.assert_not_called()

    async def test_odoo_prod_promotion_run_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_promotion_run.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            with patch(
                "control_plane.odoo_prod_promotion_http.execute_odoo_prod_promotion_run",
                side_effect=FileNotFoundError("missing manifest"),
            ):
                response = await _post_odoo_prod_promotion_run(app, self._run_payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_openapi_includes_odoo_prod_promotion_contracts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

            response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        expected = {
            "/v1/drivers/odoo/prod-promotion-inputs": (
                "write_odoo_prod_promotion_inputs",
                "OdooProdPromotionInputsEnvelope",
            ),
            "/v1/drivers/odoo/prod-promotion-run": (
                "write_odoo_prod_promotion_run",
                "OdooProdPromotionRunEnvelope",
            ),
            "/v1/drivers/odoo/prod-promotion": (
                "write_odoo_prod_promotion",
                "OdooProdPromotionEnvelope",
            ),
        }
        for route_path, (operation_id, schema_title) in expected.items():
            operation = paths[route_path]["post"]
            self.assertEqual(operation["operationId"], operation_id)
            self.assertEqual(
                operation["requestBody"]["content"]["application/json"]["schema"]["title"],
                schema_title,
            )
            self.assertEqual(
                operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            for status_code in ("400", "401", "403", "404", "409", "503"):
                self.assertIn(status_code, operation["responses"])


class FastApiOdooStableBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "cbusillo/launchplane",
        workflow_ref: str = (
            "cbusillo/launchplane/.github/workflows/odoo-stable-bootstrap.yml@refs/heads/main"
        ),
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo-tenant-cm",
        context: str = "cm",
        action: str = "odoo_stable_bootstrap.execute",
        repository: str = "cbusillo/launchplane",
        workflow_ref: str = (
            "cbusillo/launchplane/.github/workflows/odoo-stable-bootstrap.yml@refs/heads/main"
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
                        "actions": [action],
                    }
                ]
            }
        )

    def _store_with_tenant_profile(
        self, state_dir: Path, *, include_prod_lane: bool = False
    ) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        if include_prod_lane:
            lanes = list(cast(tuple[dict[str, object], ...], profile_payload["lanes"]))
            lanes.append(
                {
                    "instance": "prod",
                    "context": "cm",
                    "base_url": "https://cm.example.com",
                    "health_url": "https://cm.example.com/web/health",
                }
            )
            profile_payload["lanes"] = tuple(lanes)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _store_with_non_odoo_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _payload(
        self,
        *,
        product: str = "odoo-tenant-cm",
        instance: str = "testing",
        confirmation: str = "bootstrap cm testing",
        verify_logo: bool = True,
    ) -> dict[str, object]:
        return {
            "product": product,
            "bootstrap": {
                "product": product,
                "context": "cm",
                "instance": instance,
                "confirmation": confirmation,
                "verify_health": True,
                "verify_canonical": True,
                "verify_logo": verify_logo,
            },
        }

    async def test_odoo_stable_bootstrap_enqueues_operation_without_execution(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = self._store_with_tenant_profile(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(),
                idempotency_key="bootstrap-cm-testing",
            )
            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["status"], "accepted")
            operation_id = payload["records"]["odoo_stable_bootstrap_operation_id"]
            self.assertTrue(str(operation_id).startswith("odoo-stable-bootstrap-cm-testing-"))
            self.assertEqual(payload["result"]["status"], "pending")
            self.assertEqual(payload["result"]["phase"], "created")
            self.assertEqual(payload["result"]["request"]["confirmation"], "bootstrap cm testing")
            self.assertEqual(
                payload["result"]["poll_url"],
                f"/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}",
            )
            stored_operation = store.read_odoo_stable_bootstrap_operation_record(str(operation_id))
            self.assertEqual(stored_operation.status, "pending")
            self.assertEqual(stored_operation.phase, "created")
            self.assertEqual(stored_operation.idempotency_key, "bootstrap-cm-testing")
            self.assertEqual(stored_operation.started_at, "")
            self.assertEqual(stored_operation.finished_at, "")
            self.assertEqual(stored_operation.deployment_record_id, "")
            self.assertIsNone(stored_operation.result)

    async def test_odoo_stable_bootstrap_requires_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(app, self._payload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_odoo_stable_bootstrap_rejects_product_mismatch_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()
            bootstrap = cast(dict[str, object], request_payload["bootstrap"])
            bootstrap["product"] = "odoo-tenant-other"

            response = await _post_odoo_stable_bootstrap(
                app,
                request_payload,
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_odoo_stable_bootstrap_rejects_unknown_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(instance="missing", confirmation="bootstrap cm missing"),
                idempotency_key="bootstrap-cm-missing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_stable_bootstrap_replays_existing_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()

            first_response = await _post_odoo_stable_bootstrap(
                app,
                request_payload,
                idempotency_key="bootstrap-cm-testing",
            )
            second_response = await _post_odoo_stable_bootstrap(
                app,
                request_payload,
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(
            first_payload["records"]["odoo_stable_bootstrap_operation_id"],
            second_payload["records"]["odoo_stable_bootstrap_operation_id"],
        )
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(first_payload["result"], second_payload["result"])

    async def test_odoo_stable_bootstrap_rejects_reused_key_for_different_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            first_response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(),
                idempotency_key="bootstrap-cm-testing",
            )
            conflict_response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(verify_logo=False),
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_stable_bootstrap_reuses_idempotency_key_across_lanes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state", include_prod_lane=True)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            first_response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(instance="testing", confirmation="bootstrap cm testing"),
                idempotency_key="bootstrap-cm",
            )
            second_response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(instance="prod", confirmation="bootstrap cm prod"),
                idempotency_key="bootstrap-cm",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertNotEqual(
            first_response.json()["records"]["odoo_stable_bootstrap_operation_id"],
            second_response.json()["records"]["odoo_stable_bootstrap_operation_id"],
        )

    async def test_odoo_stable_bootstrap_blocks_second_active_lane_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()

            first_response = await _post_odoo_stable_bootstrap(
                app,
                request_payload,
                idempotency_key="bootstrap-cm-testing-1",
            )
            second_response = await _post_odoo_stable_bootstrap(
                app,
                request_payload,
                idempotency_key="bootstrap-cm-testing-2",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["error"]["code"], "odoo_stable_bootstrap_operation_active")
        self.assertEqual(
            payload["operation"]["operation_id"],
            first_response.json()["records"]["odoo_stable_bootstrap_operation_id"],
        )

    async def test_odoo_stable_bootstrap_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_target_replacement_plan.read"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(),
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_stable_bootstrap_rejects_non_odoo_product_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(),
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_stable_bootstrap_product_route_dependency_miss_is_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(),
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(payload["details"]["route_path"], "/v1/drivers/odoo/stable-bootstrap")

    async def test_odoo_stable_bootstrap_dependency_miss_precedes_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_target_replacement_plan.read"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_stable_bootstrap(
                app,
                self._payload(),
                idempotency_key="bootstrap-cm-testing",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "driver_route_dependency_not_found")

    async def test_openapi_includes_odoo_stable_bootstrap_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/v1/drivers/odoo/stable-bootstrap"]["post"]
        self.assertEqual(operation["operationId"], "write_odoo_stable_bootstrap")
        idempotency_parameters = [
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "Idempotency-Key" and parameter["in"] == "header"
        ]
        self.assertEqual(len(idempotency_parameters), 1)
        self.assertTrue(idempotency_parameters[0]["required"])
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooStableBootstrapEnvelope",
        )
        self.assertEqual(
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(status_code, operation["responses"])
        self.assertIn(
            "OdooStableBootstrapOperationActiveResponse",
            str(operation["responses"]["409"]),
        )


class FastApiOdooTargetReplacementPlanTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "cbusillo/launchplane",
        workflow_ref: str = (
            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
        ),
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo-tenant-cm",
        context: str = "cm",
        action: str = "odoo_target_replacement_plan.read",
        repository: str = "cbusillo/launchplane",
        workflow_ref: str = (
            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
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
                        "actions": [action],
                    }
                ]
            }
        )

    def _store_with_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        return store

    def _store_with_non_odoo_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _payload(
        self,
        *,
        product: str = "odoo-tenant-cm",
        instance: str = "testing",
        allow_empty_data: bool = True,
    ) -> dict[str, object]:
        return {
            "product": product,
            "replacement": {
                "product": product,
                "instance": instance,
                "strategy": "recreate-in-place",
                "allow_empty_data": allow_empty_data,
            },
        }

    def _plan(
        self, *, expected_next_target_name: str = "cm-testing"
    ) -> OdooStableTargetReplacementPlan:
        return OdooStableTargetReplacementPlan(
            plan_status="ready",
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            strategy="recreate-in-place",
            target_record_found=True,
            target_id_record_found=True,
            inventory_found=True,
            expected_next_target_name=expected_next_target_name,
        )

    async def test_odoo_target_replacement_plan_reads_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan",
                return_value=self._plan(),
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"], {})
        self.assertEqual(payload["result"]["plan_status"], "ready")
        self.assertEqual(payload["result"]["context"], "cm")
        plan_mock.assert_called_once()
        request = plan_mock.call_args.kwargs["request"]
        self.assertTrue(request.allow_empty_data)

    async def test_odoo_target_replacement_plan_recomputes_with_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan",
                side_effect=(
                    self._plan(expected_next_target_name="cm-testing-a"),
                    self._plan(expected_next_target_name="cm-testing-b"),
                ),
            ) as plan_mock:
                first_response = await _post_odoo_target_replacement_plan(
                    app,
                    self._payload(),
                    idempotency_key="replacement-plan-cm-testing",
                )
                second_response = await _post_odoo_target_replacement_plan(
                    app,
                    self._payload(),
                    idempotency_key="replacement-plan-cm-testing",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(
            first_response.json()["result"]["expected_next_target_name"], "cm-testing-a"
        )
        self.assertEqual(
            second_response.json()["result"]["expected_next_target_name"], "cm-testing-b"
        )
        self.assertEqual(plan_mock.call_count, 2)

    async def test_odoo_target_replacement_plan_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="deployment.write"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan"
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        plan_mock.assert_not_called()

    async def test_odoo_target_replacement_plan_rejects_wrong_lane_context_grant(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(context="other"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan"
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        plan_mock.assert_not_called()

    async def test_odoo_target_replacement_plan_rejects_product_mismatch_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()
            replacement = cast(dict[str, object], request_payload["replacement"])
            replacement["product"] = "odoo-tenant-other"

            response = await _post_odoo_target_replacement_plan(app, request_payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_odoo_target_replacement_plan_rejects_unknown_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan"
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(
                    app,
                    self._payload(instance="missing"),
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")
        plan_mock.assert_not_called()

    async def test_odoo_target_replacement_plan_rejects_non_odoo_product_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan"
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")
        plan_mock.assert_not_called()

    async def test_odoo_target_replacement_plan_rejects_malformed_product_profile(
        self,
    ) -> None:
        class MalformedProductProfileStore:
            def read_product_profile_record(self, product: str) -> dict[str, object]:
                return {"product": product}

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=MalformedProductProfileStore,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan"
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        plan_mock.assert_not_called()

    async def test_odoo_target_replacement_plan_maps_driver_error_to_invalid_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.build_odoo_stable_target_replacement_plan",
                side_effect=ClickException("plan rejected"),
            ) as plan_mock:
                response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        plan_mock.assert_called_once()

    async def test_odoo_target_replacement_plan_product_route_dependency_miss_is_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"], "/v1/drivers/odoo/target-replacement-plan"
        )

    async def test_odoo_target_replacement_plan_dependency_miss_precedes_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="deployment.write"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_plan(app, self._payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "driver_route_dependency_not_found")

    async def test_openapi_includes_odoo_target_replacement_plan_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/v1/drivers/odoo/target-replacement-plan"]["post"]
        self.assertEqual(operation["operationId"], "write_odoo_target_replacement_plan")
        idempotency_parameters = [
            parameter
            for parameter in operation.get("parameters", [])
            if parameter["name"] == "Idempotency-Key" and parameter["in"] == "header"
        ]
        self.assertEqual(idempotency_parameters, [])
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooTargetReplacementPlanEnvelope",
        )
        self.assertEqual(
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "503"):
            self.assertIn(status_code, operation["responses"])
        self.assertNotIn("409", operation["responses"])


class FastApiOdooTargetReplacementApplyTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "cbusillo/launchplane",
        workflow_ref: str = (
            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
        ),
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo-tenant-cm",
        context: str = "cm",
        action: str = "odoo_target_replacement_apply.execute",
        repository: str = "cbusillo/launchplane",
        workflow_refs: tuple[str, ...] = (
            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main",
        ),
    ) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": repository,
                        "workflow_refs": list(workflow_refs),
                        "event_names": ["workflow_dispatch"],
                        "products": [product],
                        "contexts": [context],
                        "actions": [action],
                    }
                ]
            }
        )

    def _store_with_tenant_profile(
        self, state_dir: Path, *, include_prod_lane: bool = False
    ) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        if include_prod_lane:
            lanes = list(cast(tuple[dict[str, object], ...], profile_payload["lanes"]))
            lanes.append(
                {
                    "instance": "prod",
                    "context": "cm",
                    "base_url": "https://cm.example.com",
                    "health_url": "https://cm.example.com/web/health",
                }
            )
            profile_payload["lanes"] = tuple(lanes)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _store_with_non_odoo_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _payload(
        self,
        *,
        product: str = "odoo-tenant-cm",
        instance: str = "testing",
        allow_empty_data: bool = False,
        verify_health: bool = False,
        verify_canonical: bool = False,
        verify_logo: bool = False,
    ) -> dict[str, object]:
        return {
            "product": product,
            "replacement": {
                "product": product,
                "instance": instance,
                "strategy": "recreate-in-place",
                "allow_empty_data": allow_empty_data,
                "verify_health": verify_health,
                "verify_canonical": verify_canonical,
                "verify_logo": verify_logo,
            },
        }

    async def test_odoo_target_replacement_apply_enqueues_operation_without_execution(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(
                    verify_health=True,
                    verify_canonical=True,
                    verify_logo=True,
                ),
                idempotency_key="apply-cm-testing",
            )

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["status"], "accepted")
            operation_id = payload["records"]["odoo_stable_target_replacement_operation_id"]
            self.assertTrue(str(operation_id).startswith("odoo-target-replacement-cm-testing-"))
            self.assertEqual(payload["result"]["status"], "pending")
            self.assertEqual(payload["result"]["phase"], "created")
            self.assertEqual(
                payload["result"]["poll_url"],
                f"/v1/drivers/odoo/target-replacement/operations/{operation_id}",
            )
            stored_operation = store.read_odoo_stable_target_replacement_operation_record(
                str(operation_id)
            )
            self.assertEqual(stored_operation.status, "pending")
            self.assertEqual(stored_operation.phase, "created")
            self.assertTrue(stored_operation.request.verify_health)
            self.assertFalse(stored_operation.request.allow_empty_data)
            self.assertEqual(stored_operation.started_at, "")
            self.assertEqual(stored_operation.finished_at, "")
            self.assertEqual(stored_operation.deployment_record_id, "")
            self.assertIsNone(stored_operation.result)

    async def test_odoo_target_replacement_apply_replays_existing_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()

            first_response = await _post_odoo_target_replacement_apply(
                app,
                request_payload,
                idempotency_key="apply-cm-testing",
            )
            second_response = await _post_odoo_target_replacement_apply(
                app,
                request_payload,
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(
            first_response.json()["records"]["odoo_stable_target_replacement_operation_id"],
            second_response.json()["records"]["odoo_stable_target_replacement_operation_id"],
        )

    async def test_odoo_target_replacement_apply_requires_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(app, self._payload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_odoo_target_replacement_apply_rejects_reused_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            first_response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(allow_empty_data=False),
                idempotency_key="apply-cm-testing",
            )
            second_response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(allow_empty_data=True),
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(second_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_target_replacement_apply_scopes_replay_to_caller(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state", include_prod_lane=True)
            policy = self._policy(
                workflow_refs=(
                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main",
                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply-other.yml@refs/heads/main",
                )
            )
            first_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            second_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply-other.yml@refs/heads/main"
                        )
                    )
                ),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            first_response = await _post_odoo_target_replacement_apply(
                first_app,
                self._payload(instance="testing"),
                idempotency_key="shared-target-replacement-key",
            )
            first_operation_id = first_response.json()["records"][
                "odoo_stable_target_replacement_operation_id"
            ]
            first_operation = store.read_odoo_stable_target_replacement_operation_record(
                first_operation_id
            )
            store.write_odoo_stable_target_replacement_operation_record(
                first_operation.model_copy(
                    update={
                        "status": "pass",
                        "phase": "completed",
                        "finished_at": "2026-05-17T00:05:00Z",
                        "updated_at": "2026-05-17T00:05:00Z",
                    }
                )
            )
            second_response = await _post_odoo_target_replacement_apply(
                second_app,
                self._payload(instance="prod"),
                idempotency_key="shared-target-replacement-key",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertNotEqual(
            first_response.json()["records"]["odoo_stable_target_replacement_operation_id"],
            second_response.json()["records"]["odoo_stable_target_replacement_operation_id"],
        )

    async def test_odoo_target_replacement_apply_cross_caller_active_key_conflicts(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            policy = self._policy(
                workflow_refs=(
                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main",
                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply-other.yml@refs/heads/main",
                )
            )
            first_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            second_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._identity(
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply-other.yml@refs/heads/main"
                        )
                    )
                ),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()

            first_response = await _post_odoo_target_replacement_apply(
                first_app,
                request_payload,
                idempotency_key="shared-active-key",
            )
            second_response = await _post_odoo_target_replacement_apply(
                second_app,
                request_payload,
                idempotency_key="shared-active-key",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(
            payload["error"]["code"], "odoo_stable_target_replacement_operation_active"
        )
        self.assertEqual(
            payload["operation"]["operation_id"],
            first_response.json()["records"]["odoo_stable_target_replacement_operation_id"],
        )
        self.assertEqual(payload["operation"]["status"], "pending")

    async def test_odoo_target_replacement_apply_blocks_second_active_lane_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()

            first_response = await _post_odoo_target_replacement_apply(
                app,
                request_payload,
                idempotency_key="apply-cm-testing-1",
            )
            second_response = await _post_odoo_target_replacement_apply(
                app,
                request_payload,
                idempotency_key="apply-cm-testing-2",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(
            payload["error"]["code"], "odoo_stable_target_replacement_operation_active"
        )
        self.assertEqual(payload["operation"]["status"], "pending")

    async def test_odoo_target_replacement_apply_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_target_replacement_plan.read"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(),
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_target_replacement_apply_rejects_wrong_lane_context_grant(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(context="other"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(),
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_target_replacement_apply_rejects_product_mismatch_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = self._payload()
            replacement = cast(dict[str, object], request_payload["replacement"])
            replacement["product"] = "odoo-tenant-other"

            response = await _post_odoo_target_replacement_apply(
                app,
                request_payload,
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_odoo_target_replacement_apply_rejects_unknown_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(instance="missing"),
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_target_replacement_apply_rejects_non_odoo_product_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(),
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_target_replacement_apply_product_route_dependency_miss_is_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_target_replacement_apply(
                app,
                self._payload(),
                idempotency_key="apply-cm-testing",
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"], "/v1/drivers/odoo/target-replacement-apply"
        )

    async def test_openapi_includes_odoo_target_replacement_apply_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/v1/drivers/odoo/target-replacement-apply"]["post"]
        self.assertEqual(operation["operationId"], "write_odoo_target_replacement_apply")
        idempotency_parameters = [
            parameter
            for parameter in operation["parameters"]
            if parameter["name"] == "Idempotency-Key" and parameter["in"] == "header"
        ]
        self.assertEqual(len(idempotency_parameters), 1)
        self.assertTrue(idempotency_parameters[0]["required"])
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooTargetReplacementApplyEnvelope",
        )
        self.assertEqual(
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(status_code, operation["responses"])


class FastApiOdooProdBackupGateTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "every/tenant-cm",
        workflow_ref: str = "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main",
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo",
        context: str = "cm",
        action: str = "odoo_prod_backup_gate.execute",
        repository: str = "every/tenant-cm",
        workflow_ref: str = "every/tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main",
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
                        "actions": [action],
                    }
                ]
            }
        )

    def _store_with_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        lanes = list(cast(tuple[dict[str, object], ...], profile_payload["lanes"]))
        lanes.append(
            {
                "instance": "prod",
                "context": "cm",
                "base_url": "https://cm.example.com",
                "health_url": "https://cm.example.com/web/health",
            }
        )
        profile_payload["lanes"] = tuple(lanes)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _store_with_non_odoo_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = self._store_with_tenant_profile(state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        lanes = list(cast(tuple[dict[str, object], ...], profile_payload["lanes"]))
        lanes.append(
            {
                "instance": "prod",
                "context": "cm",
                "base_url": "https://cm.example.com",
                "health_url": "https://cm.example.com/web/health",
            }
        )
        profile_payload["lanes"] = tuple(lanes)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _payload(
        self,
        *,
        product: str = "odoo",
        backup_record_id: str = "backup-gate-cm-prod-run-1",
    ) -> dict[str, object]:
        return {
            "product": product,
            "backup_gate": {
                "context": "cm",
                "instance": "prod",
                "backup_record_id": backup_record_id,
            },
        }

    def _result(
        self,
        *,
        backup_record_id: str = "backup-gate-cm-prod-run-1",
        backup_status: Literal["pass", "fail"] = "pass",
    ) -> OdooProdBackupGateResult:
        return OdooProdBackupGateResult(
            context="cm",
            instance="prod",
            backup_record_id=backup_record_id,
            backup_status=backup_status,
            backup_root="/volumes/data/backups/launchplane",
            database_dump_path=f"/volumes/data/backups/launchplane/cm/{backup_record_id}/cm.dump",
            filestore_archive_path=(
                f"/volumes/data/backups/launchplane/cm/{backup_record_id}/cm-filestore.tar.gz"
            ),
            manifest_path=f"/volumes/data/backups/launchplane/cm/{backup_record_id}/manifest.json",
            error_message="backup failed" if backup_status == "fail" else "",
        )

    async def test_odoo_prod_backup_gate_executes_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate",
                return_value=self._result(),
            ) as execute_mock:
                response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-backup-gate-cm",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"], {"backup_record_id": "backup-gate-cm-prod-run-1"})
        self.assertEqual(
            set(payload["result"]),
            {
                "backup_record_id",
                "backup_status",
                "backup_root",
                "database_dump_path",
                "filestore_archive_path",
                "manifest_path",
            },
        )
        self.assertEqual(payload["result"]["backup_status"], "pass")
        self.assertEqual(
            payload["result"]["database_dump_path"],
            "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-run-1/cm.dump",
        )
        execute_mock.assert_called_once()

    async def test_odoo_prod_backup_gate_accepts_product_profile_driver_id(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = self._store_with_tenant_profile(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate",
                return_value=self._result(),
            ) as execute_mock:
                response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(product="odoo-tenant-cm"),
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["records"]["backup_record_id"], "backup-gate-cm-prod-run-1"
        )
        execute_mock.assert_called_once()

    async def test_odoo_prod_backup_gate_rejects_non_odoo_product_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = self._store_with_non_odoo_profile(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate"
            ) as execute_mock:
                response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(product="odoo-tenant-cm"),
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")
        execute_mock.assert_not_called()

    async def test_odoo_prod_backup_gate_rejects_profile_lane_mismatch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = self._store_with_tenant_profile(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm", context="opw"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            payload = self._payload(product="odoo-tenant-cm")
            backup_gate = cast(dict[str, object], payload["backup_gate"])
            backup_gate["context"] = "opw"
            with patch(
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate"
            ) as execute_mock:
                response = await _post_odoo_prod_backup_gate(app, payload)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")
        execute_mock.assert_not_called()

    async def test_odoo_prod_backup_gate_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_post_deploy.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_prod_backup_gate(app, self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_prod_backup_gate_replays_idempotent_response(self) -> None:
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
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate",
                return_value=self._result(),
            ) as execute_mock:
                first_response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-backup-gate:replay",
                )
                replay_response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-backup-gate:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        execute_mock.assert_called_once()

    async def test_odoo_prod_backup_gate_rejects_idempotency_key_reuse(self) -> None:
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
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate",
                return_value=self._result(),
            ):
                first_response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-backup-gate:conflict",
                )
                conflict_response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(backup_record_id="backup-gate-cm-prod-run-2"),
                    idempotency_key="odoo-prod-backup-gate:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_prod_backup_gate_does_not_replay_failed_result(self) -> None:
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
                "control_plane.odoo_prod_backup_gate_http.execute_odoo_prod_backup_gate",
                side_effect=(self._result(backup_status="fail"), self._result()),
            ) as execute_mock:
                first_response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-backup-gate:retry-after-fail",
                )
                retry_response = await _post_odoo_prod_backup_gate(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-backup-gate:retry-after-fail",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["backup_status"], "fail")
        self.assertEqual(retry_response.status_code, 202)
        self.assertEqual(retry_response.json()["result"]["backup_status"], "pass")
        self.assertNotIn("replayed", retry_response.json())
        execute_mock.assert_called()
        self.assertEqual(execute_mock.call_count, 2)

    async def test_odoo_prod_backup_gate_product_route_dependency_miss_is_503(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_prod_backup_gate(
                app,
                self._payload(product="odoo-tenant-cm"),
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"],
            "/v1/drivers/odoo/prod-backup-gate",
        )

    async def test_odoo_prod_backup_gate_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.execute_odoo_prod_backup_gate_result",
                side_effect=FileNotFoundError,
            ):
                response = await _post_odoo_prod_backup_gate(app, self._payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_openapi_includes_odoo_prod_backup_gate_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/v1/drivers/odoo/prod-backup-gate"]["post"]
        self.assertEqual(operation["operationId"], "write_odoo_prod_backup_gate")
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooProdBackupGateEnvelope",
        )
        self.assertEqual(
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(status_code, operation["responses"])


class FastApiOdooProdRollbackTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "every/tenant-opw",
        workflow_ref: str = "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main",
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo",
        context: str = "opw",
        action: str = "odoo_prod_rollback.execute",
        repository: str = "every/tenant-opw",
        workflow_ref: str = "every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main",
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
                        "actions": [action],
                    }
                ]
            }
        )

    def _store_with_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        return store

    def _store_with_non_odoo_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _payload(self, *, product: str = "odoo") -> dict[str, object]:
        return {
            "product": product,
            "rollback": {
                "context": "opw",
                "instance": "prod",
            },
        }

    def _result(
        self,
        *,
        rollback_status: Literal["pass", "fail"] = "pass",
        deployment_record_id: str = "deployment-opw-prod-rollback",
        release_tuple_id: str = "opw-prod-artifact-opw-847c71c1db61785c",
    ) -> OdooProdRollbackResult:
        return OdooProdRollbackResult(
            context="opw",
            instance="prod",
            source_channel="testing",
            artifact_id="artifact-opw-847c71c1db61785c",
            promotion_record_id="promotion-opw-testing-to-prod",
            deployment_record_id=deployment_record_id,
            release_tuple_id=release_tuple_id,
            rollback_status=rollback_status,
            rollback_health_status="pass" if rollback_status == "pass" else "fail",
            rollback_started_at="2026-04-26T12:04:00Z",
            rollback_finished_at="2026-04-26T12:05:00Z",
            post_deploy_status="pass" if rollback_status == "pass" else "skipped",
            error_message="rollback failed" if rollback_status == "fail" else "",
        )

    async def test_odoo_prod_rollback_executes_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback",
                return_value=self._result(),
            ) as execute_mock:
                response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-rollback-opw",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {
                "promotion_record_id": "promotion-opw-testing-to-prod",
                "deployment_record_id": "deployment-opw-prod-rollback",
                "release_tuple_id": "opw-prod-artifact-opw-847c71c1db61785c",
            },
        )
        self.assertEqual(
            set(payload["result"]),
            {
                "promotion_record_id",
                "deployment_record_id",
                "release_tuple_id",
                "rollback_status",
                "rollback_health_status",
                "rollback_started_at",
                "rollback_finished_at",
                "post_deploy_status",
            },
        )
        self.assertEqual(payload["result"]["rollback_status"], "pass")
        self.assertEqual(payload["result"]["rollback_health_status"], "pass")
        self.assertEqual(payload["result"]["rollback_started_at"], "2026-04-26T12:04:00Z")
        self.assertEqual(payload["result"]["rollback_finished_at"], "2026-04-26T12:05:00Z")
        self.assertEqual(payload["result"]["post_deploy_status"], "pass")
        execute_mock.assert_called_once()

    async def test_odoo_prod_rollback_accepts_product_profile_driver_id(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback",
                return_value=self._result(),
            ) as execute_mock:
                response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(product="odoo-tenant-cm"),
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["rollback_status"], "pass")
        execute_mock.assert_called_once()

    async def test_odoo_prod_rollback_rejects_non_odoo_product_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback"
            ) as execute_mock:
                response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(product="odoo-tenant-cm"),
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")
        execute_mock.assert_not_called()

    async def test_odoo_prod_rollback_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_post_deploy.execute"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_prod_rollback(app, self._payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_prod_rollback_replays_idempotent_response(self) -> None:
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
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback",
                return_value=self._result(),
            ) as execute_mock:
                first_response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-rollback:replay",
                )
                replay_response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-rollback:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"], first_response.json()["trace_id"]
        )
        execute_mock.assert_called_once()

    async def test_odoo_prod_rollback_rejects_idempotency_key_reuse(self) -> None:
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
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback",
                return_value=self._result(),
            ):
                first_response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-rollback:conflict",
                )
                changed_payload = self._payload()
                rollback = cast(dict[str, object], changed_payload["rollback"])
                rollback["promotion_record_id"] = "promotion-opw-previous"
                conflict_response = await _post_odoo_prod_rollback(
                    app,
                    changed_payload,
                    idempotency_key="odoo-prod-rollback:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_prod_rollback_does_not_replay_failed_result(self) -> None:
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
                "control_plane.odoo_prod_rollback_http.execute_odoo_prod_rollback",
                side_effect=(self._result(rollback_status="fail"), self._result()),
            ) as execute_mock:
                first_response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-rollback:retry-after-fail",
                )
                retry_response = await _post_odoo_prod_rollback(
                    app,
                    self._payload(),
                    idempotency_key="odoo-prod-rollback:retry-after-fail",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["rollback_status"], "fail")
        self.assertEqual(retry_response.status_code, 202)
        self.assertEqual(retry_response.json()["result"]["rollback_status"], "pass")
        self.assertNotIn("replayed", retry_response.json())
        self.assertEqual(execute_mock.call_count, 2)

    async def test_odoo_prod_rollback_product_route_dependency_miss_is_503(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(product="odoo-tenant-cm"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_prod_rollback(
                app,
                self._payload(product="odoo-tenant-cm"),
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(payload["details"]["route_path"], "/v1/drivers/odoo/prod-rollback")

    async def test_odoo_prod_rollback_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.http_app.execute_odoo_prod_rollback_result",
                side_effect=FileNotFoundError,
            ):
                response = await _post_odoo_prod_rollback(app, self._payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_openapi_includes_odoo_prod_rollback_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        operation = response.json()["paths"]["/v1/drivers/odoo/prod-rollback"]["post"]
        self.assertEqual(operation["operationId"], "write_odoo_prod_rollback")
        self.assertEqual(
            operation["requestBody"]["content"]["application/json"]["schema"]["title"],
            "OdooProdRollbackEnvelope",
        )
        self.assertEqual(
            operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(status_code, operation["responses"])


class FastApiOdooPostDeployOverrideTests(unittest.IsolatedAsyncioTestCase):
    def _identity(
        self,
        *,
        repository: str = "every/tenant-opw",
        workflow_ref: str = ("every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"),
    ) -> GitHubActionsIdentity:
        return _identity(
            repository=repository,
            workflow_ref=workflow_ref,
            event_name="workflow_dispatch",
        )

    def _policy(
        self,
        *,
        product: str = "odoo",
        context: str = "opw",
        action: str = "odoo_post_deploy.execute",
        repository: str = "every/tenant-opw",
        workflow_ref: str = ("every/tenant-opw/.github/workflows/deploy-odoo.yml@refs/heads/main"),
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
                        "actions": [action],
                    }
                ]
            }
        )

    def _tenant_identity(self, *, workflow_name: str) -> GitHubActionsIdentity:
        workflow_ref = f"cbusillo/launchplane/.github/workflows/{workflow_name}@refs/heads/main"
        return self._identity(
            repository="cbusillo/launchplane",
            workflow_ref=workflow_ref,
        )

    def _tenant_policy(
        self,
        *,
        action: str,
        workflow_name: str,
    ) -> LaunchplaneAuthzPolicy:
        workflow_ref = f"cbusillo/launchplane/.github/workflows/{workflow_name}@refs/heads/main"
        return self._policy(
            product="odoo-tenant-cm",
            context="cm",
            action=action,
            repository="cbusillo/launchplane",
            workflow_ref=workflow_ref,
        )

    def _store_with_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        )
        return store

    def _store_with_non_odoo_tenant_profile(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        profile_payload = _odoo_preview_profile_payload()
        profile_payload["driver_id"] = "generic-web"
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile_payload)
        )
        return store

    def _config_override_payload(self, *, key: str = "web.base.url") -> dict[str, object]:
        return {
            "product": "odoo-tenant-cm",
            "override": {
                "product": "odoo-tenant-cm",
                "context": "cm",
                "instance": "testing",
                "key": key,
                "value": "https://cm-testing.shinycomputers.com",
            },
        }

    def _website_override_payload(self) -> dict[str, object]:
        return {
            "product": "odoo-tenant-cm",
            "override": {
                "product": "odoo-tenant-cm",
                "context": "cm",
                "instance": "testing",
                "website_bootstrap": {
                    "tenant": "cm",
                    "name": "Cell Mechanic",
                    "canonical_url": "https://cm-testing.shinycomputers.com",
                    "homepage_url": "/cell-mechanic",
                    "logo_path": "addons/cm_website/static/src/img/logo.png",
                    "logo_alt": "Cell Mechanic",
                    "routes": [
                        {
                            "name": "Cell Mechanic",
                            "url": "/cell-mechanic",
                            "module": "cm_website",
                            "homepage": True,
                        }
                    ],
                },
            },
        }

    async def test_odoo_app_maintenance_executes_authorized_post_deploy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity(workflow_name="deploy-odoo.yml")),
                authz_policy=self._tenant_policy(
                    action="odoo_app_maintenance.execute",
                    workflow_name="deploy-odoo.yml",
                ),
                record_store_factory=lambda: self._store_with_tenant_profile(root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_app_maintenance_http.execute_odoo_app_maintenance",
                return_value=OdooAppMaintenanceResult(
                    maintenance_status="pass",
                    action="post-deploy",
                    intent="stable-post-deploy",
                    context="cm",
                    instance="testing",
                    post_deploy_status="pass",
                    override_status="pass",
                    override_record_found=True,
                    override_payload_rendered=True,
                    applied_at="2026-04-26T12:05:00Z",
                    started_at="2026-04-26T12:04:00Z",
                    finished_at="2026-04-26T12:05:00Z",
                ),
            ):
                response = await _post_odoo_app_maintenance(
                    app,
                    {
                        "product": "odoo-tenant-cm",
                        "maintenance": {
                            "context": "cm",
                            "instance": "testing",
                            "action": "post-deploy",
                            "intent": "stable-post-deploy",
                        },
                    },
                    idempotency_key="odoo-app-maintenance:cm-testing-post-deploy",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["transition"],
            "odoo-app-maintenance:cm:testing:deploy",
        )
        self.assertEqual(payload["result"]["maintenance_status"], "pass")
        self.assertEqual(payload["result"]["post_deploy_status"], "pass")

    async def test_odoo_app_maintenance_does_not_replay_failed_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity(workflow_name="deploy-odoo.yml")),
                authz_policy=self._tenant_policy(
                    action="odoo_app_maintenance.execute",
                    workflow_name="deploy-odoo.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            request_payload: dict[str, object] = {
                "product": "odoo-tenant-cm",
                "maintenance": {
                    "context": "cm",
                    "instance": "testing",
                    "action": "post-deploy",
                    "intent": "stable-post-deploy",
                },
            }
            with patch(
                "control_plane.odoo_app_maintenance_http.execute_odoo_app_maintenance",
                side_effect=(
                    OdooAppMaintenanceResult(
                        maintenance_status="fail",
                        action="post-deploy",
                        intent="stable-post-deploy",
                        context="cm",
                        instance="testing",
                        post_deploy_status="fail",
                        override_status="fail",
                        error_message="temporary Odoo maintenance failure",
                    ),
                    OdooAppMaintenanceResult(
                        maintenance_status="pass",
                        action="post-deploy",
                        intent="stable-post-deploy",
                        context="cm",
                        instance="testing",
                        post_deploy_status="pass",
                        override_status="pass",
                    ),
                ),
            ) as execute_mock:
                first_response = await _post_odoo_app_maintenance(
                    app,
                    request_payload,
                    idempotency_key="odoo-app-maintenance:retry-after-failure",
                )
                retry_response = await _post_odoo_app_maintenance(
                    app,
                    request_payload,
                    idempotency_key="odoo-app-maintenance:retry-after-failure",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.json()["result"]["maintenance_status"], "fail")
        self.assertEqual(retry_response.status_code, 202)
        self.assertFalse(retry_response.json().get("replayed", False))
        self.assertEqual(retry_response.json()["result"]["maintenance_status"], "pass")
        self.assertEqual(execute_mock.call_count, 2)

    async def test_odoo_app_maintenance_rejects_non_deploy_phase(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity(workflow_name="deploy-odoo.yml")),
                authz_policy=self._tenant_policy(
                    action="odoo_app_maintenance.execute",
                    workflow_name="deploy-odoo.yml",
                ),
                record_store_factory=lambda: self._store_with_tenant_profile(root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_app_maintenance(
                app,
                {
                    "product": "odoo-tenant-cm",
                    "maintenance": {
                        "context": "cm",
                        "instance": "testing",
                        "action": "post-deploy",
                        "intent": "stable-post-deploy",
                        "phase": "promotion",
                    },
                },
                idempotency_key="odoo-app-maintenance:unsupported-phase",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_odoo_app_maintenance_rejects_unsupported_action(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._tenant_identity(workflow_name="deploy-odoo.yml")),
                authz_policy=self._tenant_policy(
                    action="odoo_app_maintenance.execute",
                    workflow_name="deploy-odoo.yml",
                ),
                record_store_factory=lambda: self._store_with_tenant_profile(root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_app_maintenance(
                app,
                {
                    "product": "odoo-tenant-cm",
                    "maintenance": {
                        "context": "cm",
                        "instance": "testing",
                        "action": "reset-testing",
                        "intent": "stable-post-deploy",
                    },
                },
                idempotency_key="odoo-app-maintenance:unsupported-action",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_odoo_post_deploy_executes_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_post_deploy_http.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                    override_status="pass",
                    override_record_found=True,
                    override_payload_rendered=True,
                    applied_at="2026-04-26T12:05:00Z",
                ),
            ) as execute_mock:
                response = await _post_odoo_post_deploy(
                    app,
                    {
                        "product": "odoo",
                        "post_deploy": {
                            "context": "opw",
                            "instance": "testing",
                            "phase": "deploy",
                        },
                    },
                    idempotency_key="odoo-post-deploy:opw-testing-deploy",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {"transition": "odoo-post-deploy:opw:testing:deploy"},
        )
        self.assertEqual(payload["result"]["post_deploy_status"], "pass")
        self.assertEqual(payload["result"]["override_status"], "pass")
        execute_mock.assert_called_once()

    async def test_odoo_post_deploy_replays_idempotent_response(self) -> None:
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
                "control_plane.odoo_post_deploy_http.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                    override_status="pass",
                    override_record_found=True,
                    override_payload_rendered=True,
                    applied_at="2026-04-26T12:05:00Z",
                ),
            ) as execute_mock:
                request_payload: dict[str, object] = {
                    "product": "odoo",
                    "post_deploy": {"context": "opw", "instance": "testing"},
                }
                first_response = await _post_odoo_post_deploy(
                    app,
                    request_payload,
                    idempotency_key="odoo-post-deploy:replay",
                )
                replay_response = await _post_odoo_post_deploy(
                    app,
                    request_payload,
                    idempotency_key="odoo-post-deploy:replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        execute_mock.assert_called_once()

    async def test_odoo_post_deploy_rejects_idempotency_key_reuse(self) -> None:
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
                "control_plane.odoo_post_deploy_http.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                    override_status="pass",
                    override_record_found=True,
                    override_payload_rendered=True,
                    applied_at="2026-04-26T12:05:00Z",
                ),
            ):
                first_response = await _post_odoo_post_deploy(
                    app,
                    {
                        "product": "odoo",
                        "post_deploy": {"context": "opw", "instance": "testing"},
                    },
                    idempotency_key="odoo-post-deploy:conflict",
                )
                conflict_response = await _post_odoo_post_deploy(
                    app,
                    {
                        "product": "odoo",
                        "post_deploy": {"context": "opw", "instance": "prod"},
                    },
                    idempotency_key="odoo-post-deploy:conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_odoo_post_deploy_handler_file_miss_is_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.odoo_post_deploy_http.execute_odoo_post_deploy",
                side_effect=FileNotFoundError("missing manifest"),
            ):
                response = await _post_odoo_post_deploy(
                    app,
                    {
                        "product": "odoo",
                        "post_deploy": {"context": "opw", "instance": "testing"},
                    },
                    idempotency_key="odoo-post-deploy:file-miss",
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_odoo_post_deploy_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="deployment.write"),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_post_deploy(
                app,
                {
                    "product": "odoo",
                    "post_deploy": {"context": "opw", "instance": "testing"},
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_config_parameter_override_writes_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_config_parameter_override.write",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url",
            )
            stored_record = store.read_odoo_instance_override_record(
                context_name="cm", instance_name="testing"
            )

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["records"], {})
            self.assertEqual(payload["result"]["context"], "cm")
            self.assertEqual(payload["result"]["instance"], "testing")
            self.assertEqual(payload["result"]["config_parameter_keys"], ["web.base.url"])
            self.assertEqual(stored_record.config_parameters[0].key, "web.base.url")
            self.assertEqual(
                stored_record.config_parameters[0].value.value,
                "https://cm-testing.shinycomputers.com",
            )
            self.assertEqual(stored_record.apply_on, ("deploy", "promotion"))

    async def test_odoo_config_parameter_override_preserves_existing_phases(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="cm",
                    instance="testing",
                    apply_on=("manual",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal", value="https://old.example.com"
                            ),
                        ),
                    ),
                    updated_at="2026-05-10T20:00:00Z",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_config_parameter_override.write",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url",
            )
            stored_record = store.read_odoo_instance_override_record(
                context_name="cm", instance_name="testing"
            )

            self.assertEqual(response.status_code, 202)
            self.assertEqual(stored_record.apply_on, ("manual", "deploy", "promotion"))
            self.assertEqual(
                stored_record.config_parameters[0].value.value,
                "https://cm-testing.shinycomputers.com",
            )

    async def test_odoo_config_parameter_override_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_config_parameter_override.write",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            first_response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url:replay",
            )
            replay_response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url:replay",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])

    async def test_odoo_config_parameter_override_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_post_deploy.execute",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url:unauthorized",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_odoo_config_parameter_override_dependency_miss_is_dependency_503(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_config_parameter_override.write",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
                control_plane_root_path=root,
            )

            response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url:dependency-miss",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "driver_route_dependency_not_found")
        self.assertEqual(
            payload["details"]["route_path"],
            "/v1/drivers/odoo/config-parameter-override",
        )

    async def test_odoo_config_parameter_override_rejects_non_odoo_product_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_non_odoo_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_config_parameter_override.write",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(),
                idempotency_key="odoo-cm-testing-web-base-url:non-odoo",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_odoo_config_parameter_override_rejects_unsupported_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-config-parameter-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_config_parameter_override.write",
                    workflow_name="odoo-config-parameter-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_config_parameter_override(
                app,
                self._config_override_payload(key="database.secret"),
                idempotency_key="odoo-cm-testing-unsupported",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_odoo_website_bootstrap_override_writes_typed_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store_with_tenant_profile(root / "state")
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="cm",
                    instance="testing",
                    apply_on=("manual",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal",
                                value="https://cm-testing.shinycomputers.com",
                            ),
                        ),
                    ),
                    updated_at="2026-05-10T20:00:00Z",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    self._tenant_identity(workflow_name="odoo-website-bootstrap-override.yml")
                ),
                authz_policy=self._tenant_policy(
                    action="odoo_website_bootstrap_override.write",
                    workflow_name="odoo-website-bootstrap-override.yml",
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_odoo_website_bootstrap_override(
                app,
                self._website_override_payload(),
                idempotency_key="odoo-cm-testing-website-bootstrap",
            )
            stored_record = store.read_odoo_instance_override_record(
                context_name="cm", instance_name="testing"
            )

            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["result"]["website_bootstrap"], True)
            self.assertEqual(stored_record.apply_on, ("manual", "deploy", "promotion"))
            self.assertEqual(stored_record.config_parameters[0].key, "web.base.url")
            self.assertIsNotNone(stored_record.website_bootstrap)
            assert stored_record.website_bootstrap is not None
            self.assertEqual(stored_record.website_bootstrap.name, "Cell Mechanic")
            self.assertEqual(
                stored_record.website_bootstrap.canonical_url,
                "https://cm-testing.shinycomputers.com",
            )

    async def test_openapi_includes_odoo_post_deploy_override_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(self._identity()),
            authz_policy=self._policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        expected_routes = {
            "/v1/drivers/odoo/post-deploy": (
                "write_odoo_post_deploy",
                "OdooPostDeployEnvelope",
            ),
            "/v1/drivers/odoo/app-maintenance": (
                "write_odoo_app_maintenance",
                "OdooAppMaintenanceEnvelope",
            ),
            "/v1/drivers/odoo/config-parameter-override": (
                "write_odoo_config_parameter_override",
                "OdooConfigParameterOverrideEnvelope",
            ),
            "/v1/drivers/odoo/website-bootstrap-override": (
                "write_odoo_website_bootstrap_override",
                "OdooWebsiteBootstrapOverrideEnvelope",
            ),
        }
        for route_path, (operation_id, schema_title) in expected_routes.items():
            route = paths[route_path]["post"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["requestBody"]["content"]["application/json"]["schema"]["title"],
                schema_title,
            )
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            for status_code in ("400", "401", "403", "404", "409", "503"):
                self.assertIn(status_code, route["responses"])


if __name__ == "__main__":
    unittest.main()
