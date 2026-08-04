import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from unittest.mock import patch

from click import ClickException

from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.drivers import generic_web_preview_dispatch
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewVerificationRequest,
)
from control_plane.generic_web_promotion_http import GenericWebProdPromotionResponse
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime, idempotency_request_fingerprint
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy import GenericWebDeployResult
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyResult,
    GenericWebPreviewInventoryItem,
    GenericWebPreviewInventoryResult,
    GenericWebPreviewReadinessCheck,
    GenericWebPreviewReadinessResult,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
    GenericWebPreviewTransportSummary,
)
from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionResult
from control_plane.workflows.generic_web_rollback import GenericWebRollbackApplyResult
from control_plane.workflows.odoo_generic_web_post_deploy import (
    execute_odoo_generic_web_post_deploy,
)
from tests.support.auth import _StubVerifier, _identity
from tests.support.profiles import (
    _odoo_preview_profile_payload,
    _odoo_profile_payload_with_prod_lane,
    _product_profile_payload,
    _product_profile_payload_with_prod,
)
from tests.support.stores import (
    _seed_generic_web_deploy_target_records,
    _sqlite_database_url,
)
from tests.test_service import (
    _fastapi_browser_mutation_headers,
    _fastapi_human_session_manager,
    _fastapi_signed_in_cookie,
    _http_request_for_service_test,
    _invoke_app,
    _product_profile_lanes,
    create_launchplane_fastapi_test_app,
)


def _generic_web_deploy_result(
    *,
    deployment_record_id: str = "deployment-syo-testing",
    deploy_status: Literal["pass", "fail"] = "pass",
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped",
    error_message: str = "",
) -> GenericWebDeployResult:
    return GenericWebDeployResult(
        deployment_record_id=deployment_record_id,
        deploy_status=deploy_status,
        deploy_started_at="2026-05-26T02:00:00Z",
        deploy_finished_at="2026-05-26T02:05:00Z",
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="testing",
        target_name="syo-testing",
        target_id="app-syo-testing",
        target_category="application",
        provider_id="dokploy",
        provider_target_type="application",
        post_deploy_status=post_deploy_status,
        error_message=error_message,
    )


class GenericWebHttpTests(unittest.TestCase):
    def test_generic_web_preview_refresh_uses_current_runtime_authz_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            identity = _identity(
                repository="cbusillo/sellyouroutboard",
                workflow_ref=(
                    "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                    "@refs/heads/main"
                ),
            )
            denied_policy = LaunchplaneAuthzPolicy()
            allowed_policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": identity.repository,
                            "workflow_refs": [identity.workflow_ref],
                            "event_names": [identity.event_name],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(denied_policy)
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=denied_policy,
                authz_policy_runtime=authz_policy_runtime,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }
            authz_policy_runtime.update(allowed_policy, revision=2)

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                },
            ) as refresh:
                granted_status_code, _ = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:grant"},
                )
                authz_policy_runtime.update(denied_policy, revision=3)
                revoked_status_code, revoked_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:revoke"},
                )

        self.assertEqual(granted_status_code, 202)
        refresh.assert_called_once()
        self.assertEqual(revoked_status_code, 403)
        self.assertEqual(revoked_payload["error"]["code"], "authorization_denied")

    def test_terminal_agent_read_token_rejects_non_read_routes_even_if_policy_grants_action(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "terminal_agents": [
                        {
                            "subjects": ["local-owner-agent"],
                            "token_labels": ["local-owner-read"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                authorization="Bearer terminal-read-token",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertIn("can only read", payload["error"]["message"])

    def test_generic_web_deploy_route_uses_profile_lane_for_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-syo-testing",
                target_name="syo-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = _generic_web_deploy_result()

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-testing"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
        self.assertEqual(kwargs["lane"].context, "sellyouroutboard-testing")

    def test_generic_web_deploy_route_accepts_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            profile_payload = _product_profile_payload()
            profile_payload["driver_id"] = "odoo"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-syo-testing",
                target_name="syo-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = _generic_web_deploy_result(
                deployment_record_id="deployment-odoo-testing"
            )

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-derived-driver"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-odoo-testing")
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].driver_id, "odoo")
        self.assertEqual(kwargs["lane"].context, "sellyouroutboard-testing")
        self.assertIs(
            kwargs["post_deploy_executor"],
            execute_odoo_generic_web_post_deploy,
        )

    def test_generic_web_deploy_route_keeps_literal_generic_products_without_post_deploy_adapter(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-syo-testing",
                target_name="syo-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = _generic_web_deploy_result()

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-no-adapter"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()
        self.assertIsNone(deploy.call_args.kwargs["post_deploy_executor"])

    def test_generic_web_deploy_route_replays_post_deploy_failure_after_deploy_pass(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-syo-testing",
                target_name="syo-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = GenericWebDeployResult(
                deployment_record_id="deployment-syo-testing-post-deploy-failed",
                deploy_status="pass",
                deploy_started_at="2026-05-26T02:00:00Z",
                deploy_finished_at="2026-05-26T02:05:00Z",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                target_name="syo-testing",
                target_id="app-syo-testing",
                target_category="application",
                provider_id="dokploy",
                provider_target_type="application",
                post_deploy_status="fail",
                error_message="post-deploy failed after deploy passed",
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "deploy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            }

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-post-deploy-failed"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-post-deploy-failed"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(
            second_payload["records"]["deployment_record_id"], driver_result.deployment_record_id
        )
        self.assertTrue(second_payload["replayed"])
        deploy.assert_called_once()

    def test_generic_web_deploy_route_replay_scrubs_retired_target_type_alias(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-syo-testing",
                target_name="syo-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            identity = _identity()
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = GenericWebDeployResult(
                deployment_record_id="deployment-syo-testing-retired-alias",
                deploy_status="pass",
                deploy_started_at="2026-05-26T02:00:00Z",
                deploy_finished_at="2026-05-26T02:05:00Z",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                target_name="syo-testing",
                target_id="app-syo-testing",
                target_category="application",
                provider_id="dokploy",
                provider_target_type="application",
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "deploy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            }

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-retired-alias"},
                )
                idempotency_record = store.read_idempotency_record(
                    scope="|".join(
                        (
                            identity.repository,
                            identity.workflow_ref or identity.job_workflow_ref,
                            identity.subject,
                        )
                    ),
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="generic-web-deploy-retired-alias",
                )
                self.assertIsNotNone(idempotency_record)
                assert idempotency_record is not None
                legacy_response_payload = idempotency_record.response_payload
                legacy_result_payload = legacy_response_payload.get("result")
                self.assertIsInstance(legacy_result_payload, dict)
                assert isinstance(legacy_result_payload, dict)
                legacy_result_payload["target_type"] = "application"
                legacy_records_payload = legacy_response_payload.get("records")
                self.assertIsInstance(legacy_records_payload, dict)
                assert isinstance(legacy_records_payload, dict)
                legacy_records_payload["target_type"] = "application"
                store.write_idempotency_record(
                    idempotency_record.model_copy(
                        update={"response_payload": legacy_response_payload}, deep=True
                    )
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-retired-alias"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertNotIn("target_type", first_payload["result"])
        self.assertEqual(second_status_code, 202)
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["result"]["target_category"], "application")
        self.assertEqual(second_payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", second_payload["records"])
        self.assertNotIn("target_type", second_payload["result"])
        deploy.assert_called_once()

    def test_generic_web_deploy_route_rejects_unknown_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            profile_payload = _product_profile_payload()
            profile_payload["driver_id"] = "missing-driver"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy"
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
        deploy.assert_not_called()

    def test_generic_web_source_ref_deploy_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/source-ref-deploy",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "deploy": {
                        "schema_version": 1,
                        "context": "sellyouroutboard-testing",
                        "instance": "testing",
                        "source_git_ref": "abc123",
                        "provider_source_ref": "refs/heads/launchplane-deploy/abc123",
                    },
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_generic_web_deploy_route_accepts_padded_lane_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            profile_payload = _product_profile_payload()
            profile_payload["lanes"] = tuple(
                {**lane, "context": f"  {lane['context']}  "}
                for lane in _product_profile_lanes(profile_payload)
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-syo-testing",
                target_name="syo-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = _generic_web_deploy_result()

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-padded-context"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()

    def test_generic_web_rollback_plan_route_writes_plan_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            deployment_record = DeploymentRecord(
                record_id="deployment-syo-prod-previous",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
                ),
                context="sellyouroutboard-testing",
                instance="prod",
                source_git_ref="abc123",
                destination_health=HealthcheckEvidence(status="pass"),
                resolved_target=ResolvedTargetEvidence(
                    target_type="application",
                    target_id="app-prod",
                    target_name="syo-prod-app",
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-app",
                    target_type="application",
                    deploy_mode="dokploy-application-api",
                    deployment_id="deployment-provider-1",
                    status="pass",
                    started_at="2026-05-25T12:00:00Z",
                    finished_at="2026-05-25T12:01:00Z",
                ),
            )
            store.write_deployment_record(deployment_record)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
                headers={"Idempotency-Key": "generic-web-rollback-plan-syo-prod"},
            )

            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=1,
            )
            plan = plans[0]

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["generic_web_rollback_plan_id"], plan.plan_id)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.product, "sellyouroutboard")
        self.assertEqual(plan.context, "sellyouroutboard-testing")
        self.assertEqual(plan.rollback_deployment_record_id, "deployment-syo-prod-previous")

    def test_generic_web_rollback_plan_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_rollback_plan_route_rejects_unknown_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "missing",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")

    def test_generic_web_rollback_plan_route_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-syo-prod-previous",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
                    ),
                    context="sellyouroutboard-testing",
                    instance="prod",
                    source_git_ref="abc123",
                    destination_health=HealthcheckEvidence(status="pass"),
                    resolved_target=ResolvedTargetEvidence(
                        target_type="application",
                        target_id="app-prod",
                        target_name="syo-prod-app",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="syo-prod-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="deployment-provider-1",
                        status="pass",
                        started_at="2026-05-25T12:00:00Z",
                        finished_at="2026-05-25T12:01:00Z",
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "rollback_plan": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "prod",
                    "rollback_deployment_record_id": "deployment-syo-prod-previous",
                },
            }

            first_status_code, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "generic-web-rollback-plan-syo-prod"},
            )
            second_status_code, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "generic-web-rollback-plan-syo-prod"},
            )
            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=10,
            )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(len(plans), 1)

    def test_generic_web_rollback_route_applies_ready_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod",
                deployment_record_id="deployment-syo-prod-rollback",
                rollback_status="pass",
                deploy_status="pass",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
            )

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "rollback": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "prod",
                            "rollback_deployment_record_id": "deployment-syo-prod-previous",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-rollback-syo-prod"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["generic_web_rollback_plan_id"],
            "generic-web-rollback-syo-prod",
        )
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-prod-rollback")
        self.assertEqual(payload["records"]["rollback_status"], "pass")
        self.assertEqual(payload["records"]["deploy_status"], "pass")
        self.assertEqual(payload["records"]["post_deploy_status"], "skipped")
        rollback.assert_called_once()
        self.assertEqual(rollback.call_args.kwargs["request"].product, "sellyouroutboard")
        self.assertIsNone(rollback.call_args.kwargs["post_deploy_executor"])

    def test_generic_web_rollback_route_passes_odoo_post_deploy_adapter_for_odoo_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _odoo_profile_payload_with_prod_lane()
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-cm-prod",
                deployment_record_id="deployment-cm-prod-rollback",
                rollback_status="pass",
                deploy_status="pass",
                product="odoo-tenant-cm",
                context="cm",
                instance="prod",
                rollback_deployment_record_id="deployment-cm-prod-previous",
            )

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload={
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "rollback": {
                            "schema_version": 1,
                            "product": "odoo-tenant-cm",
                            "instance": "prod",
                            "rollback_deployment_record_id": "deployment-cm-prod-previous",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-rollback-cm-prod"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-cm-prod-rollback")
        self.assertEqual(payload["records"]["post_deploy_status"], "skipped")
        rollback.assert_called_once()
        self.assertIs(
            rollback.call_args.kwargs["post_deploy_executor"],
            execute_odoo_generic_web_post_deploy,
        )

    def test_generic_web_rollback_route_replays_idempotent_response_shape(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod",
                deployment_record_id="deployment-syo-prod-rollback",
                rollback_status="pass",
                deploy_status="pass",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "rollback": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "prod",
                    "rollback_deployment_record_id": "deployment-syo-prod-previous",
                },
            }

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-rollback-replay-syo-prod"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-rollback-replay-syo-prod"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(second_payload["records"]["rollback_status"], "pass")
        self.assertEqual(second_payload["records"]["deploy_status"], "pass")
        self.assertEqual(second_payload["records"]["post_deploy_status"], "skipped")
        self.assertTrue(second_payload["replayed"])
        rollback.assert_called_once()

    def test_generic_web_rollback_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_deploy_route_resolves_literal_generic_web_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _product_profile_payload("generic-web")
                )
            )
            _seed_generic_web_deploy_target_records(
                store=store,
                context="generic-web-testing",
                instance="testing",
                target_id="app-generic-web-testing",
                target_name="generic-web-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["generic-web"],
                            "contexts": ["generic-web-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = GenericWebDeployResult(
                deployment_record_id="deployment-generic-web-testing",
                deploy_status="pass",
                deploy_started_at="2026-05-26T02:00:00Z",
                deploy_finished_at="2026-05-26T02:05:00Z",
                product="generic-web",
                context="generic-web-testing",
                instance="testing",
                target_name="generic-web-testing",
                target_id="app-generic-web-testing",
                target_category="application",
                provider_id="dokploy",
                provider_target_type="application",
            )

            with patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "generic-web",
                        "deploy": {
                            "schema_version": 1,
                            "product": "generic-web",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/generic-web@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-literal-driver"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["deployment_record_id"], "deployment-generic-web-testing"
        )
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].product, "generic-web")
        self.assertEqual(kwargs["lane"].context, "generic-web-testing")

    def test_generic_web_deploy_route_rejects_wrong_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/deploy",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "deploy": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_prod_promotion_live_requires_intent_or_unreviewed_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/sellyouroutboard",
                                "workflow_refs": [
                                    "cbusillo/sellyouroutboard/.github/workflows/"
                                    "promote-prod.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["sellyouroutboard"],
                                "contexts": ["sellyouroutboard-testing"],
                                "actions": ["generic_web_prod_promotion.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
                headers={"Idempotency-Key": "generic-web-prod-promotion-reviewed-only"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "promotion_intent_required")

    def test_generic_web_prod_promotion_unreviewed_live_requires_database(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            identity = _identity(
                repository="cbusillo/sellyouroutboard",
                workflow_ref=(
                    "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                ),
                event_name="workflow_dispatch",
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/sellyouroutboard",
                                "workflow_refs": [identity.workflow_ref],
                                "event_names": ["workflow_dispatch"],
                                "products": ["sellyouroutboard"],
                                "contexts": ["sellyouroutboard-testing"],
                                "actions": [
                                    "generic_web_prod_promotion.execute",
                                    "generic_web_prod_promotion.execute_unreviewed",
                                ],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
                headers={"Idempotency-Key": "generic-web-prod-promotion-unreviewed"},
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    def test_generic_web_prod_promotion_route_executes_for_authorized_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_id="app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                            "dry_run": True,
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-syo"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["promotion_record_id"], "promotion-syo-testing-to-prod")
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-prod")
        self.assertEqual(payload["records"]["inventory_record_id"], "sellyouroutboard-testing-prod")
        self.assertEqual(payload["result"]["source_health_status"], "pass")
        self.assertEqual(payload["result"]["destination_health_status"], "pass")
        self.assertEqual(payload["result"]["target_category"], "application")
        self.assertEqual(payload["result"]["provider_id"], "dokploy")
        self.assertEqual(payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", payload["result"])
        GenericWebProdPromotionResponse.model_validate(payload)
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "promotion": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                    "dry_run": True,
                },
            }

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_id="app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-replay"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-replay"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(first_payload["result"], second_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertNotIn("target_type", second_payload["result"])
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_replays_dry_run_pending_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "promotion": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                    "dry_run": True,
                },
            }

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="",
                    inventory_record_id="",
                    promotion_status="pending",
                    deployment_status="skipped",
                    backup_status="skipped",
                    source_health_status="pending",
                    destination_health_status="pending",
                    dry_run=True,
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-dry-run"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-dry-run"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertTrue(first_payload["result"]["dry_run"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(second_payload["records"]["dry_run"], "True")
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_does_not_replay_failed_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "promotion": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                    "dry_run": True,
                },
            }

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="",
                    inventory_record_id="",
                    promotion_status="fail",
                    deployment_status="fail",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    error_message="Deployment failed.",
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-fail"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-fail"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["result"]["promotion_status"], "fail")
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(execute_mock.call_count, 2)

    def test_generic_web_prod_promotion_rejects_terminal_agent_bearer(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"terminal_agents": []}),
                control_plane_root_path=root,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="terminal-agent",
                    terminal_agent_token_label="terminal-agent-read",
                ),
            )

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion"
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    authorization="Bearer terminal-agent-token",
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        execute_mock.assert_not_called()

    def test_generic_web_prod_promotion_route_rejects_wrong_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_prod_promotion_route_accepts_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["driver_id"] = "odoo"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-odoo-testing-to-prod",
                    deployment_record_id="deployment-odoo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="odoo-prod-app",
                    target_id="app-odoo",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                            "dry_run": True,
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-odoo"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["promotion_record_id"], "promotion-odoo-testing-to-prod"
        )
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-odoo-prod")
        self.assertEqual(payload["result"]["target_category"], "application")
        self.assertEqual(payload["result"]["provider_id"], "dokploy")
        self.assertEqual(payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", payload["result"])
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_accepts_padded_lane_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["lanes"] = tuple(
                {**lane, "context": f"  {lane['context']}  "}
                for lane in _product_profile_lanes(profile_payload)
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_id="app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                            "dry_run": True,
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-syo-padded"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["promotion_record_id"], "promotion-syo-testing-to-prod")
        self.assertEqual(payload["result"]["target_category"], "application")
        self.assertEqual(payload["result"]["provider_id"], "dokploy")
        self.assertEqual(payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", payload["result"])
        execute_mock.assert_called_once()

    def test_human_session_can_dry_run_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_manager = _fastapi_human_session_manager()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            with patch(
                "control_plane.generic_web_promotion_http.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="",
                    inventory_record_id="",
                    promotion_status="pending",
                    deployment_status="skipped",
                    backup_status="skipped",
                    source_health_status="pending",
                    destination_health_status="pending",
                    dry_run=True,
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                            "dry_run": True,
                        },
                    },
                    authorization="",
                    headers=_fastapi_browser_mutation_headers(session_manager, cookie),
                )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["deployment_status"], "skipped")
        self.assertEqual(payload["records"]["deployment_record_id"], "")
        self.assertEqual(payload["records"]["inventory_record_id"], "")
        execute_mock.assert_called_once()

    def test_human_session_cannot_live_execute_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_manager = _fastapi_human_session_manager()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": [
                                "generic_web_prod_promotion.execute",
                                "generic_web_prod_promotion.execute_unreviewed",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers=_fastapi_browser_mutation_headers(session_manager, cookie),
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_cannot_replay_live_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_manager = _fastapi_human_session_manager()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "promotion": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                    "dry_run": False,
                },
            }
            idempotency_key = "generic-web-prod-promotion-human-live"
            store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id="idempotency-generic-web-prod-promotion-human-live",
                    scope="|".join(("github-human", "alice", "123")),
                    route_path="/v1/drivers/generic-web/prod-promotion",
                    idempotency_key=idempotency_key,
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path="/v1/drivers/generic-web/prod-promotion",
                        payload=request_payload,
                    ),
                    response_status_code=202,
                    response_trace_id="generic-web-prod-promotion-human-live",
                    recorded_at="2026-06-05T22:00:00Z",
                    response_payload={
                        "status": "accepted",
                        "trace_id": "generic-web-prod-promotion-human-live",
                        "records": {"promotion_record_id": "promotion-live"},
                        "result": {"promotion_status": "pass", "dry_run": False},
                    },
                )
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload=request_payload,
                authorization="",
                headers={
                    **_fastapi_browser_mutation_headers(session_manager, cookie),
                    "Idempotency-Key": idempotency_key,
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_must_use_product_owned_promotion_workflow_route(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            session_manager = _fastapi_human_session_manager()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                database_url=database_url,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "  sellyouroutboard-testing  ",
                        "dry_run": False,
                        "bump": "patch",
                        "observe_timeout_seconds": 0,
                    },
                },
                authorization="",
                headers=_fastapi_browser_mutation_headers(session_manager, cookie),
            )
            outbox_rows = store.list_outbox_delivery_records(states=("pending",))
            store.close()

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(outbox_rows, ())

    def test_human_session_cannot_bypass_product_route_with_padded_lane_context(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            session_manager = _fastapi_human_session_manager()
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["lanes"] = tuple(
                {**lane, "context": f"  {lane['context']}  "}
                for lane in _product_profile_lanes(profile_payload)
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                database_url=database_url,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "dry_run": False,
                        "bump": "patch",
                        "observe_timeout_seconds": 0,
                    },
                },
                authorization="",
                headers=_fastapi_browser_mutation_headers(session_manager, cookie),
            )
            outbox_rows = store.list_outbox_delivery_records(states=("pending",))
            store.close()

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(outbox_rows, ())

    def test_generic_web_promotion_workflow_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                database_url=database_url,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "workflow": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "dry_run": False,
                },
            }

            first_status_code, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload=request_payload,
                headers={"Idempotency-Key": "generic-web-promotion-workflow-replay"},
            )
            second_status_code, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload=request_payload,
                headers={"Idempotency-Key": "generic-web-promotion-workflow-replay"},
            )
            outbox_rows = store.list_outbox_delivery_records(states=("pending",))
            store.close()

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertIn("outbox_delivery_id", first_payload["records"])
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].delivery_id, first_payload["records"]["outbox_delivery_id"])
        self.assertNotIn("previous_run_ids", outbox_rows[0].payload)
        self.assertNotIn("dispatch_started_at", outbox_rows[0].payload)
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    def test_generic_web_promotion_workflow_rejects_terminal_agent_bearer(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"terminal_agents": []}),
                control_plane_root_path=root,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="terminal-agent",
                    terminal_agent_token_label="terminal-agent-read",
                ),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "dry_run": False,
                    },
                },
                authorization="Bearer terminal-agent-token",
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_promotion_workflow_rejects_unauthorized_human(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_manager = _fastapi_human_session_manager()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_humans": []}),
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers=_fastapi_browser_mutation_headers(session_manager, cookie),
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_promotion_workflow_accepts_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["driver_id"] = "odoo"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                database_url=database_url,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "dry_run": False,
                    },
                },
            )
            outbox_rows = store.list_outbox_delivery_records(states=("pending",))
            store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["dispatch_status"], "pending")
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].aggregate_id, "sellyouroutboard:sellyouroutboard-testing")

    def test_generic_web_promotion_workflow_rejects_human_before_context_resolution(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_manager = _fastapi_human_session_manager()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["unowned-context"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "unowned-context",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers=_fastapi_browser_mutation_headers(session_manager, cookie),
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_promotion_workflow_rejects_token_unowned_context_before_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["unowned-context"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "unowned-context",
                        "dry_run": False,
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")

    def test_generic_web_preview_inventory_route_writes_scan_from_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = GenericWebPreviewInventoryResult(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                source="generic-web-preview-inventory",
                app_name_prefix="syo-pr-",
                previews=(
                    GenericWebPreviewInventoryItem(
                        applicationId="app-42",
                        applicationName="syo-pr-42",
                        previewSlug="pr-42",
                    ),
                ),
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_inventory",
                return_value=driver_result,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-inventory",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "inventory": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )
            records = FilesystemRecordStore(
                state_dir=state_dir
            ).list_preview_inventory_scan_records(context_name="sellyouroutboard-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["preview_inventory_scan_id"],
            records[0].scan_id,
        )
        self.assertEqual(records[0].source, "generic-web-preview-inventory")
        self.assertEqual(records[0].preview_slugs, ("pr-42",))

    def test_generic_web_preview_inventory_does_not_replay_cached_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload: dict[str, object] = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "inventory": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                },
            }
            idempotency_key = "generic-web-preview-inventory:syo"
            store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id="idempotency-stale-generic-web-preview-inventory",
                    scope="|".join(
                        (
                            "cbusillo/sellyouroutboard",
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main",
                            "repo:every/verireel:pull_request",
                        )
                    ),
                    route_path="/v1/drivers/generic-web/preview-inventory",
                    idempotency_key=idempotency_key,
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path="/v1/drivers/generic-web/preview-inventory",
                        payload=request_payload,
                    ),
                    response_status_code=202,
                    response_trace_id="stale-generic-web-preview-inventory",
                    recorded_at="2026-05-09T15:08:00Z",
                    response_payload={
                        "status": "accepted",
                        "trace_id": "stale-generic-web-preview-inventory",
                        "records": {"preview_inventory_scan_id": "stale-scan"},
                        "result": {"previews": [{"previewSlug": "stale"}]},
                    },
                )
            )
            driver_results = [
                GenericWebPreviewInventoryResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    source="generic-web-preview-inventory",
                    app_name_prefix="syo-pr-",
                    previews=(
                        GenericWebPreviewInventoryItem(
                            applicationId="app-42",
                            applicationName="syo-pr-42",
                            previewSlug="pr-42",
                        ),
                    ),
                ),
                GenericWebPreviewInventoryResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    source="generic-web-preview-inventory",
                    app_name_prefix="syo-pr-",
                    previews=(),
                ),
            ]

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_inventory",
                side_effect=driver_results,
            ) as execute_inventory:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-inventory",
                    payload=request_payload,
                    headers={"Idempotency-Key": idempotency_key},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-inventory",
                    payload=request_payload,
                    headers={"Idempotency-Key": idempotency_key},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["result"]["previews"][0]["previewSlug"], "pr-42")
        self.assertEqual(second_payload["result"]["previews"], [])
        self.assertNotIn("replayed", first_payload)
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(execute_inventory.call_count, 2)

    def test_generic_web_preview_inventory_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_inventory",
            ) as execute_inventory:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-inventory",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "inventory": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        execute_inventory.assert_not_called()

    def test_generic_web_preview_refresh_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "preview-42",
                    "application_name": "sellyouroutboard-preview-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                },
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

                self.assertEqual(status_code, 202)
                self.assertEqual(payload["records"]["transition"], "verifying")
                self.assertEqual(payload["result"]["refresh_status"], "pass")
                self.assertEqual(payload["result"]["application_id"], "app-preview")
                store = FilesystemRecordStore(state_dir=state_dir)
                preview = store.read_preview_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42"
                )
                generation = store.read_preview_generation_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42-generation-0001"
                )
                self.assertEqual(preview.state, "pending")
                self.assertEqual(generation.state, "verifying")
                self.assertEqual(generation.deploy_status, "pass")
                self.assertEqual(generation.verify_status, "pending")
                refresh.assert_called_once()
                _, kwargs = refresh.call_args
                self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
                self.assertEqual(kwargs["request"].preview_url, "https://pr-42.example.test")

    def test_generic_web_preview_refresh_keeps_health_responsive_during_provider_wait(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile = LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            profile = profile.model_copy(
                update={"preview": profile.preview.model_copy(update={"slug_template": "preview-{number}"})}
            )
            store.write_product_profile_record(
                profile
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            provider_wait_started = threading.Event()
            release_provider_wait = threading.Event()
            provider_wait_started_at: list[float] = []
            provider_refresh_calls: list[None] = []

            def _slow_refresh(**_kwargs: object) -> dict[str, object]:
                provider_refresh_calls.append(None)
                provider_wait_started_at.append(time.monotonic())
                provider_wait_started.set()
                release_provider_wait.wait(timeout=0.75)
                return {
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "preview-42",
                    "application_name": "sellyouroutboard-preview-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                }

            async def _exercise_refresh_and_health() -> tuple[Any, Any, Any, float]:
                refresh_task = asyncio.create_task(
                    _http_request_for_service_test(
                        app,
                        method="POST",
                        path="/v1/drivers/generic-web/preview-refresh",
                        payload={
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "refresh": {
                                "schema_version": 1,
                                "product": "sellyouroutboard",
                                "anchor_pr_number": 42,
                                "preview_url": "https://pr-42.example.test",
                                "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                            },
                        },
                        authorization="Bearer valid-token",
                        headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                    )
                )
                await asyncio.wait_for(
                    asyncio.to_thread(provider_wait_started.wait, 1), timeout=1.25
                )
                competing_response = await asyncio.wait_for(
                    _http_request_for_service_test(
                        app,
                        method="POST",
                        path="/v1/drivers/generic-web/preview-refresh",
                        payload={
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "refresh": {
                                "schema_version": 1,
                                "product": "sellyouroutboard",
                                "preview_slug": "preview-42",
                                "preview_url": "https://pr-42.example.test",
                                "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                            },
                        },
                        authorization="Bearer valid-token",
                        headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42-competing"},
                    ),
                    timeout=0.5,
                )
                health_response = await asyncio.wait_for(
                    _http_request_for_service_test(
                        app,
                        method="GET",
                        path="/v1/health",
                    ),
                    timeout=0.5,
                )
                health_elapsed_seconds = time.monotonic() - provider_wait_started_at[0]
                release_provider_wait.set()
                refresh_response = await asyncio.wait_for(refresh_task, timeout=1)
                return refresh_response, competing_response, health_response, health_elapsed_seconds

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                side_effect=_slow_refresh,
            ):
                (
                    refresh_response,
                    competing_response,
                    health_response,
                    health_elapsed_seconds,
                ) = asyncio.run(_exercise_refresh_and_health())

        self.assertEqual(refresh_response.status_code, 202)
        self.assertEqual(competing_response.status_code, 409)
        self.assertEqual(competing_response.json()["error"]["code"], "mutation_in_progress")
        self.assertEqual(health_response.status_code, 200)
        self.assertLess(health_elapsed_seconds, 0.5)
        self.assertEqual(len(provider_refresh_calls), 1)

    def test_generic_web_preview_refresh_mutation_builder_records_smoke_failure(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
        driver_result = GenericWebPreviewRefreshResult.model_validate(
            {
                "refresh_status": "fail",
                "refresh_started_at": "2026-05-03T15:00:00Z",
                "refresh_finished_at": "2026-05-03T15:05:00Z",
                "product": "sellyouroutboard",
                "context": "sellyouroutboard-testing",
                "preview_slug": "pr-42",
                "application_name": "sellyouroutboard-pr-42",
                "application_id": "app-preview",
                "preview_url": "https://pr-42.example.test",
                "runtime_identity": {
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "instance": "pr-42",
                    "environment_kind": "preview",
                    "deployment_record_id": "deployment-pr-42",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard:sha",
                    "source_git_ref": "abc123",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                    "preview_id": "pr-42",
                },
                "smoke": {
                    "smoke_status": "fail",
                    "checked_at": "2026-05-03T15:04:55Z",
                    "checks": [
                        {
                            "check_id": "health",
                            "status": "fail",
                            "message": "Health check failed.",
                        }
                    ],
                    "failure_summary": "Smoke failed on /api/health.",
                },
            }
        )
        preview_request, generation_request = (
            generic_web_preview_dispatch._generic_web_preview_refresh_mutation_requests(
                request=GenericWebPreviewRefreshRequest.model_validate(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "preview_slug": "pr-42",
                        "preview_url": "https://pr-42.request.example.test",
                        "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        "anchor_head_sha": "abc123",
                    }
                ),
                driver_result=driver_result,
                profile=profile,
            )
        )

        self.assertEqual(preview_request.state, "failed")
        self.assertEqual(preview_request.canonical_url, "https://pr-42.example.test")
        self.assertEqual(generation_request.state, "failed")
        self.assertEqual(generation_request.deploy_status, "pass")
        self.assertEqual(generation_request.verify_status, "fail")
        self.assertEqual(generation_request.failure_stage, "verify")
        self.assertEqual(generation_request.failure_summary, "Smoke failed on /api/health.")
        self.assertEqual(generation_request.runtime_identity, driver_result.runtime_identity)

    def test_generic_web_preview_refresh_route_accepts_omitted_preview_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.syo-preview.example.test",
                },
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["preview_url"], "https://pr-42.syo-preview.example.test")
        refresh.assert_called_once()
        _, kwargs = refresh.call_args
        self.assertEqual(kwargs["request"].preview_url, "")

    def test_generic_web_preview_refresh_route_persists_provider_failure_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "fail",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                    "error_message": "Dokploy API POST /api/application.update failed (500): provider exploded",
                },
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

                self.assertEqual(status_code, 202)
                self.assertEqual(payload["records"]["transition"], "failed")
                self.assertEqual(payload["result"]["refresh_status"], "fail")
                store = FilesystemRecordStore(state_dir=state_dir)
                preview = store.read_preview_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42"
                )
                generation = store.read_preview_generation_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42-generation-0001"
                )
                self.assertEqual(preview.state, "failed")
                self.assertEqual(generation.state, "failed")
                self.assertEqual(generation.deploy_status, "fail")
                self.assertEqual(generation.verify_status, "skipped")
                self.assertEqual(generation.failure_stage, "provision")
                self.assertEqual(
                    generation.failure_summary,
                    "Dokploy API POST /api/application.update failed (500): provider exploded",
                )

    def test_generic_web_preview_verification_route_accepts_odoo_base_driver_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-cm-odoo-tenant-cm-pr-42",
                    context="cm",
                    anchor_repo="odoo-tenant-cm",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/odoo-tenant-cm/pull/42",
                    preview_label="preview",
                    canonical_url="https://pr-42.cm-preview.example.test",
                    state="pending",
                    created_at="2026-05-09T15:00:00Z",
                    updated_at="2026-05-09T15:05:00Z",
                    eligible_at="2026-05-09T15:00:00Z",
                    active_generation_id="preview-cm-odoo-tenant-cm-pr-42-generation-0001",
                    latest_generation_id="preview-cm-odoo-tenant-cm-pr-42-generation-0001",
                    latest_manifest_fingerprint="odoo-preview-manifest-pr-42-abc123",
                )
            )
            store.write_preview_generation_record(
                PreviewGenerationRecord(
                    generation_id="preview-cm-odoo-tenant-cm-pr-42-generation-0001",
                    preview_id="preview-cm-odoo-tenant-cm-pr-42",
                    sequence=1,
                    state="verifying",
                    requested_reason="external_preview_refresh",
                    requested_at="2026-05-09T15:00:00Z",
                    started_at="2026-05-09T15:00:00Z",
                    resolved_manifest_fingerprint="odoo-preview-manifest-pr-42-abc123",
                    artifact_id="ghcr.io/cbusillo/odoo-tenant-cm:sha",
                    anchor_summary=PreviewPullRequestSummary(
                        repo="odoo-tenant-cm",
                        pr_number=42,
                        head_sha="abc123",
                        pr_url="https://github.com/cbusillo/odoo-tenant-cm/pull/42",
                    ),
                    deploy_status="pass",
                    verify_status="pending",
                    overall_health_status="pending",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/preview.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/preview-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "anchor_repo": "odoo-tenant-cm",
                        "anchor_pr_number": 42,
                        "verification_status": "pass",
                        "verified_at": "2026-05-09T15:08:00Z",
                        "checked_urls": ["https://pr-42.cm-preview.example.test/web/health"],
                        "timeout_seconds": 30,
                    },
                },
                headers={"Idempotency-Key": "generic-preview-verification:cm:42:run-1"},
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["records"]["transition"], "ready")
            self.assertEqual(payload["records"]["preview_state"], "active")
            self.assertEqual(payload["records"]["verification_status"], "pass")
            self.assertEqual(
                payload["records"]["generic_web_preview_verification"]["checked_urls"],
                ["https://pr-42.cm-preview.example.test/web/health"],
            )
            preview = store.read_preview_record("preview-cm-odoo-tenant-cm-pr-42")
            generation = store.read_preview_generation_record(
                "preview-cm-odoo-tenant-cm-pr-42-generation-0001"
            )
            self.assertEqual(preview.state, "active")
            self.assertEqual(preview.serving_generation_id, generation.generation_id)
            self.assertEqual(generation.state, "ready")
            self.assertEqual(generation.verify_status, "pass")
            self.assertEqual(generation.overall_health_status, "pass")

    def test_generic_web_preview_verification_route_does_not_require_lane(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload()
            profile_payload["lanes"] = ()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_verification_http._apply_generic_web_preview_verification_records",
                return_value={
                    "transition": "ready",
                    "preview_state": "active",
                    "verification_status": "pass",
                    "generic_web_preview_verification": {"checked_urls": ()},
                },
            ) as apply_records:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "verification": {
                            "schema_version": 1,
                            "anchor_repo": "sellyouroutboard",
                            "anchor_pr_number": 42,
                            "verification_status": "pass",
                            "verified_at": "2026-05-09T15:08:00Z",
                        },
                    },
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:no-lane"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["transition"], "ready")
        apply_records.assert_called_once()
        self.assertEqual(apply_records.call_args.kwargs["control_plane_root_path"], root)
        self.assertEqual(
            apply_records.call_args.kwargs["request"].context,
            "sellyouroutboard-testing",
        )

    def test_generic_web_preview_verification_route_rejects_unauthorized_context(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_verification_http._apply_generic_web_preview_verification_records",
                return_value={"transition": "ready"},
            ) as apply_records:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "verification": {
                            "schema_version": 1,
                            "context": "sellyouroutboard-testing",
                            "anchor_repo": "sellyouroutboard",
                            "anchor_pr_number": 42,
                            "verification_status": "pass",
                            "verified_at": "2026-05-09T15:08:00Z",
                        },
                    },
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:denied"},
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        apply_records.assert_not_called()

    def test_generic_web_preview_verification_rejects_context_not_owned_by_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_verification_http._apply_generic_web_preview_verification_records",
                return_value={"transition": "ready"},
            ) as apply_records:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "verification": {
                            "schema_version": 1,
                            "context": "other-context",
                            "anchor_repo": "sellyouroutboard",
                            "anchor_pr_number": 42,
                            "verification_status": "pass",
                            "verified_at": "2026-05-09T15:08:00Z",
                        },
                    },
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:bad-context"},
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
        apply_records.assert_not_called()

    def test_generic_web_preview_verification_replay_revalidates_preview_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "verification": {
                    "schema_version": 1,
                    "context": "sellyouroutboard-testing",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 42,
                    "verification_status": "pass",
                    "verified_at": "2026-05-09T15:08:00Z",
                },
            }

            with patch(
                "control_plane.generic_web_verification_http._apply_generic_web_preview_verification_records",
                return_value={"transition": "ready"},
            ) as apply_records:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:replay"},
                )
                disabled_payload = dict(profile_payload)
                disabled_payload["preview"] = {"enabled": False}
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(disabled_payload)
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:replay"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["records"]["transition"], "ready")
        self.assertEqual(second_status_code, 400)
        self.assertEqual(second_payload["error"]["code"], "invalid_request")
        apply_records.assert_called_once()

    def test_generic_web_preview_verification_failed_result_is_not_cached(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "verification": {
                    "schema_version": 1,
                    "context": "sellyouroutboard-testing",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 42,
                    "verification_status": "fail",
                    "verified_at": "2026-05-09T15:08:00Z",
                },
            }

            with patch(
                "control_plane.generic_web_verification_http._apply_generic_web_preview_verification_records",
                side_effect=(
                    {"transition": "failed", "verification_status": "fail"},
                    {"transition": "ready", "verification_status": "pass"},
                ),
            ) as apply_records:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:fail-retry"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:fail-retry"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["records"]["verification_status"], "fail")
        self.assertEqual(second_status_code, 202)
        self.assertEqual(second_payload["records"]["verification_status"], "pass")
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(apply_records.call_count, 2)

    def test_generic_web_preview_verification_request_accepts_explicit_url_collections(
        self,
    ) -> None:
        base_payload = {
            "schema_version": 1,
            "context": "cm",
            "anchor_repo": "odoo-tenant-cm",
            "anchor_pr_number": 42,
            "verification_status": "pass",
            "verified_at": "2026-05-09T15:08:00Z",
            "timeout_seconds": 30,
        }

        list_request = GenericWebPreviewVerificationRequest.model_validate(
            {
                **base_payload,
                "checked_urls": [" https://pr-42.cm-preview.example.test/web/health "],
            }
        )
        tuple_request = GenericWebPreviewVerificationRequest.model_validate(
            {
                **base_payload,
                "checked_urls": ("https://pr-42.cm-preview.example.test/cell-mechanic",),
            }
        )

        self.assertEqual(
            list_request.checked_urls,
            ("https://pr-42.cm-preview.example.test/web/health",),
        )
        self.assertEqual(
            tuple_request.checked_urls,
            ("https://pr-42.cm-preview.example.test/cell-mechanic",),
        )

    def test_generic_web_preview_verification_records_reject_missing_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            request = GenericWebPreviewVerificationRequest.model_validate(
                {
                    "schema_version": 1,
                    "context": "cm",
                    "anchor_repo": "odoo-tenant-cm",
                    "anchor_pr_number": 42,
                    "verification_status": "pass",
                    "verified_at": "2026-05-09T15:08:00Z",
                }
            )

            with self.assertRaises(ClickException) as raised:
                generic_web_preview_dispatch._apply_generic_web_preview_verification_records(
                    control_plane_root_path=root,
                    record_store=store,
                    request=request,
                )

        self.assertEqual(
            str(raised.exception),
            "No Launchplane preview found for cm/odoo-tenant-cm/pr-42.",
        )

    def test_generic_web_preview_verification_records_reject_missing_generation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-cm-odoo-tenant-cm-pr-42",
                    context="cm",
                    anchor_repo="odoo-tenant-cm",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/odoo-tenant-cm/pull/42",
                    preview_label="preview",
                    canonical_url="https://pr-42.cm-preview.example.test",
                    state="pending",
                    created_at="2026-05-09T15:00:00Z",
                    updated_at="2026-05-09T15:05:00Z",
                    eligible_at="2026-05-09T15:00:00Z",
                )
            )
            request = GenericWebPreviewVerificationRequest.model_validate(
                {
                    "schema_version": 1,
                    "context": "cm",
                    "anchor_repo": "odoo-tenant-cm",
                    "anchor_pr_number": 42,
                    "verification_status": "pass",
                    "verified_at": "2026-05-09T15:08:00Z",
                }
            )

            with self.assertRaises(ClickException) as raised:
                generic_web_preview_dispatch._apply_generic_web_preview_verification_records(
                    control_plane_root_path=root,
                    record_store=store,
                    request=request,
                )

        self.assertEqual(
            str(raised.exception),
            "No Launchplane preview generation found for preview-cm-odoo-tenant-cm-pr-42.",
        )

    def test_generic_web_preview_refresh_route_keeps_blocked_result_non_mutating(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "blocked",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:00:01Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "",
                    "preview_url": "https://pr-42.example.test",
                    "error_message": "Generic web preview readiness blocked refresh.",
                },
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )

                self.assertEqual(status_code, 202)
                self.assertEqual(payload["records"], {})
                self.assertEqual(payload["result"]["refresh_status"], "blocked")
                store = FilesystemRecordStore(state_dir=state_dir)
                self.assertEqual(store.list_preview_records(), ())
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope=(
                            "github-actions:cbusillo/sellyouroutboard:"
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                        route_path="/v1/drivers/generic-web/preview-refresh",
                        idempotency_key="generic-web-preview-refresh:syo:42:sha",
                    )
                )
                refresh.assert_called_once()

    def test_generic_web_preview_refresh_retry_runs_again_after_blocked_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                side_effect=[
                    {
                        "refresh_status": "blocked",
                        "refresh_started_at": "2026-05-03T15:00:00Z",
                        "refresh_finished_at": "2026-05-03T15:00:01Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "",
                        "preview_url": "https://pr-42.example.test",
                        "error_message": "Generic web preview readiness blocked refresh.",
                    },
                    {
                        "refresh_status": "pass",
                        "refresh_started_at": "2026-05-03T15:06:00Z",
                        "refresh_finished_at": "2026-05-03T15:10:00Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "app-preview",
                        "preview_url": "https://pr-42.example.test",
                    },
                ],
            ) as refresh:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["result"]["refresh_status"], "blocked")
        self.assertEqual(second_status_code, 202)
        self.assertEqual(second_payload["result"]["refresh_status"], "pass")
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(refresh.call_count, 2)

    def test_generic_web_preview_refresh_route_rejects_unparseable_slug_before_provider_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh"
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "custom-preview",
                            "preview_url": "https://custom-preview.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:custom"},
                )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        refresh.assert_not_called()

    def test_generic_web_preview_refresh_retry_runs_again_after_failed_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                side_effect=[
                    {
                        "refresh_status": "fail",
                        "refresh_started_at": "2026-05-03T15:00:00Z",
                        "refresh_finished_at": "2026-05-03T15:05:00Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "app-preview",
                        "preview_url": "https://pr-42.example.test",
                        "error_message": "provider unavailable",
                    },
                    {
                        "refresh_status": "pass",
                        "refresh_started_at": "2026-05-03T15:06:00Z",
                        "refresh_finished_at": "2026-05-03T15:10:00Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "app-preview",
                        "preview_url": "https://pr-42.example.test",
                    },
                ],
            ) as refresh:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["result"]["refresh_status"], "fail")
        self.assertEqual(second_status_code, 202)
        self.assertEqual(second_payload["result"]["refresh_status"], "pass")
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(refresh.call_count, 2)

    def test_generic_web_preview_refresh_rejects_reused_key_for_changed_artifact(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            first_request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha-a",
                },
            }
            second_request_payload = json.loads(json.dumps(first_request_payload))
            second_request_payload["refresh"]["image_reference"] = (
                "ghcr.io/cbusillo/sellyouroutboard:sha-b"
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                },
            ) as refresh:
                first_status_code, _ = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=first_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha-a"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=second_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha-a"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 409)
        self.assertEqual(second_payload["error"]["code"], "idempotency_key_reused")
        refresh.assert_called_once()

    def test_generic_web_preview_readiness_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_readiness.evaluate"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.evaluate_generic_web_preview_readiness",
                return_value=GenericWebPreviewReadinessResult(
                    readiness_status="blocked",
                    checked_at="2026-05-09T15:08:00Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    template_context="sellyouroutboard-testing",
                    template_instance="testing",
                    source="generic-web-preview-readiness",
                    missing_template_env_keys=("SMTP_HOST",),
                    missing_provider_fields=(),
                    transport=GenericWebPreviewTransportSummary(
                        data_transport_mode="none",
                        copied_env_keys=(),
                        omitted_env_keys=(),
                        override_env_keys=(),
                        preview_url_env_keys=(),
                        preview_domain_env_keys=(),
                        migration_command_configured=False,
                        seed_command_configured=False,
                    ),
                    checks=(
                        GenericWebPreviewReadinessCheck(
                            check_id="template-env",
                            status="blocked",
                            message="Missing template env keys.",
                        ),
                    ),
                ),
            ) as readiness:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-readiness",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "readiness": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["readiness_status"], "blocked")
        self.assertEqual(payload["result"]["missing_template_env_keys"], ["SMTP_HOST"])
        readiness.assert_called_once()
        _, kwargs = readiness.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")

    def test_generic_web_preview_readiness_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["preview_readiness.evaluate"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.evaluate_generic_web_preview_readiness",
            ) as readiness:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-readiness",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "readiness": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        readiness.assert_not_called()

    def test_generic_web_preview_destroy_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_destroy",
                return_value=GenericWebPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-05-03T16:00:00Z",
                    destroy_finished_at="2026-05-03T16:00:02Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    preview_slug="pr-42",
                    application_name="sellyouroutboard-pr-42",
                    application_id="app-preview",
                ),
            ) as destroy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-destroy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "destroy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "destroy_reason": "external_preview_pull_request_closed",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-destroy:syo:pr-42"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["destroy_status"], "pass")
        self.assertEqual(payload["result"]["application_id"], "app-preview")
        destroy.assert_called_once()
        _, kwargs = destroy.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
        self.assertEqual(kwargs["profile"].preview.context, "sellyouroutboard-testing")
        self.assertEqual(kwargs["request"].preview_slug, "pr-42")

    def test_generic_web_preview_destroy_replays_when_only_reason_changes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            first_request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "destroy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "destroy_reason": "external_preview_pull_request_closed",
                },
            }
            second_request_payload = json.loads(json.dumps(first_request_payload))
            second_request_payload["destroy"]["destroy_reason"] = "janitor_backstop"

            with patch(
                "control_plane.generic_web_preview_http.execute_generic_web_preview_destroy",
                return_value=GenericWebPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-05-03T16:00:00Z",
                    destroy_finished_at="2026-05-03T16:00:02Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    preview_slug="pr-42",
                    application_name="sellyouroutboard-pr-42",
                    application_id="app-preview",
                ),
            ) as destroy:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-destroy",
                    payload=first_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-destroy:syo:42"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-destroy",
                    payload=second_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-destroy:syo:42"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["result"], second_payload["result"])
        self.assertTrue(second_payload["replayed"])
        destroy.assert_called_once()

    def test_generic_web_stable_verification_route_accepts_odoo_base_driver_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "success",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://cm-testing.example.com/web/health"],
                        "timeout_seconds": 45,
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:1"},
            )

            self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                    "inventory_record_id": "cm-testing",
                },
            )
            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")
            inventory = store.read_environment_inventory(context_name="cm", instance_name="testing")
            self.assertEqual(deployment.destination_health.status, "pass")
            self.assertEqual(
                deployment.destination_health.urls,
                ("https://cm-testing.example.com/web/health",),
            )
            self.assertEqual(inventory.deployment_record_id, deployment.record_id)

    def test_generic_web_stable_verification_records_runtime_identity_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            runtime_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=runtime_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=runtime_identity.artifact_id
                    ),
                    context=runtime_identity.context,
                    instance=runtime_identity.instance,
                    source_git_ref=runtime_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=runtime_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": runtime_identity.context,
                        "instance": runtime_identity.instance,
                        "deployment_record_id": runtime_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {
                            "status": "ok",
                            "version": runtime_identity.artifact_id,
                            "runtime_identity": runtime_identity.model_dump(mode="json"),
                        },
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:syo:testing:identity"},
            )

            deployment = store.read_deployment_record(runtime_identity.deployment_record_id)

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "pass")
        self.assertEqual(deployment.destination_health.structured_health.status, "pass")
        self.assertEqual(
            deployment.destination_health.structured_health.version,
            runtime_identity.artifact_id,
        )
        self.assertEqual(deployment.destination_health.runtime_identity_status, "match")
        self.assertEqual(deployment.destination_health.observed_runtime_identity, runtime_identity)

    def test_generic_web_stable_verification_fails_runtime_identity_mismatch(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            expected_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            observed_identity = expected_identity.model_copy(
                update={"artifact_id": "ghcr.io/every/sellyouroutboard@sha256:stale"}
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=expected_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=expected_identity.artifact_id
                    ),
                    context=expected_identity.context,
                    instance=expected_identity.instance,
                    source_git_ref=expected_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=expected_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": expected_identity.context,
                        "instance": expected_identity.instance,
                        "deployment_record_id": expected_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {
                            "status": "ok",
                            "version": observed_identity.artifact_id,
                            "runtime_identity": observed_identity.model_dump(mode="json"),
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:identity-mismatch"
                },
            )

            deployment = store.read_deployment_record(expected_identity.deployment_record_id)

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_generic_web_stable_verification_fails_when_identity_payload_missing(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            expected_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=expected_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=expected_identity.artifact_id
                    ),
                    context=expected_identity.context,
                    instance=expected_identity.instance,
                    source_git_ref=expected_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=expected_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": expected_identity.context,
                        "instance": expected_identity.instance,
                        "deployment_record_id": expected_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:identity-missing-payload"
                },
            )

            deployment = store.read_deployment_record(expected_identity.deployment_record_id)

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "fail")
        self.assertEqual(deployment.destination_health.runtime_identity_status, "missing")

    def test_generic_web_stable_verification_rejects_passing_payload_without_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            expected_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=expected_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=expected_identity.artifact_id
                    ),
                    context=expected_identity.context,
                    instance=expected_identity.instance,
                    source_git_ref=expected_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=expected_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": expected_identity.context,
                        "instance": expected_identity.instance,
                        "deployment_record_id": expected_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {"status": "ok"},
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:identity-missing"
                },
            )

            deployment = store.read_deployment_record(expected_identity.deployment_record_id)

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_generic_web_stable_verification_fails_structured_health_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-syo-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
                    ),
                    context="sellyouroutboard-testing",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": "sellyouroutboard-testing",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-syo-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {
                            "status": "not_ready",
                            "version": "2026.04.20",
                            "summary": "last sync is stale",
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:structured-fail"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-syo-testing")

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "fail")
        self.assertEqual(deployment.destination_health.structured_health.status, "fail")
        self.assertEqual(deployment.destination_health.structured_health.version, "2026.04.20")
        self.assertEqual(
            deployment.destination_health.structured_health.detail,
            "last sync is stale",
        )

    def test_generic_web_stable_verification_requires_timeout_for_checked_urls(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://cm-testing.example.com/web/health"],
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:missing-timeout"
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_generic_web_stable_verification_updates_linked_promotion_health(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260420T153500Z-cm-prod-to-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    deployment_record_id="deployment-20260420T153000Z-cm-testing",
                    backup_record_id="backup-cm-prod-20260420T152500Z",
                    context="cm",
                    from_instance="prod",
                    to_instance="testing",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-promote",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "promotion_record_id": "promotion-20260420T153500Z-cm-prod-to-testing",
                        "verification_status": "fail",
                        "verified_at": "2026-04-20T15:35:00Z",
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:promotion"},
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")
            promotion = store.read_promotion_record("promotion-20260420T153500Z-cm-prod-to-testing")
            inventory = store.read_environment_inventory(context_name="cm", instance_name="testing")

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(
            payload["records"]["deployment_record_id"],
            "deployment-20260420T153000Z-cm-testing",
        )
        self.assertEqual(
            payload["records"]["promotion_record_id"],
            "promotion-20260420T153500Z-cm-prod-to-testing",
        )
        self.assertEqual(payload["records"]["inventory_record_id"], "cm-testing")
        self.assertEqual(deployment.destination_health.status, "fail")
        self.assertEqual(promotion.destination_health.status, "fail")
        self.assertEqual(inventory.deployment_record_id, deployment.record_id)
        self.assertEqual(inventory.promotion_record_id, promotion.record_id)
        self.assertEqual(inventory.promoted_from_instance, "prod")

    def test_generic_web_stable_verification_validates_promotion_before_writes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260420T153500Z-cm-prod-to-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    deployment_record_id="",
                    backup_record_id="backup-cm-prod-20260420T152500Z",
                    context="cm",
                    from_instance="prod",
                    to_instance="testing",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-promote",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "promotion_record_id": "promotion-20260420T153500Z-cm-prod-to-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:mismatch"},
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")
            promotion = store.read_promotion_record("promotion-20260420T153500Z-cm-prod-to-testing")

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")
        self.assertEqual(promotion.destination_health.status, "pending")

    def test_generic_web_stable_verification_failed_result_is_not_cached(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            base_verification: dict[str, object] = {
                "schema_version": 1,
                "context": "cm",
                "instance": "testing",
                "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                "verified_at": "2026-04-20T15:35:00Z",
            }
            base_payload = {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "verification": base_verification,
            }
            failed_payload = {
                **base_payload,
                "verification": {
                    **base_verification,
                    "verification_status": "fail",
                },
            }
            passed_payload = {
                **base_payload,
                "verification": {
                    **base_verification,
                    "verification_status": "pass",
                },
            }

            first_status_code, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload=failed_payload,
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:fail-retry"},
            )
            second_status_code, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload=passed_payload,
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:fail-retry"},
            )

        self.assertEqual(first_status_code, 202, msg=json.dumps(first_payload, indent=2))
        self.assertEqual(first_payload["result"]["deployment_health_status"], "fail")
        self.assertEqual(second_status_code, 202, msg=json.dumps(second_payload, indent=2))
        self.assertEqual(second_payload["result"]["deployment_health_status"], "pass")
        self.assertNotIn("replayed", second_payload)

    def test_generic_web_stable_verification_evaluates_health_payload_runtime_identity(
        self,
    ) -> None:
        from control_plane.contracts.runtime_identity import RuntimeIdentity

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                    runtime_identity=RuntimeIdentity(
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        environment_kind="stable",
                        deployment_record_id="deployment-20260420T153000Z-cm-testing",
                        artifact_id="artifact-20260420-a1b2c3d4",
                        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                        image_reference="artifact-20260420-a1b2c3d4",
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "health_payload": {
                            "launchplaneRuntimeIdentity": {
                                "product": "odoo-tenant-cm",
                                "context": "cm",
                                "instance": "testing",
                                "environment_kind": "stable",
                                "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                                "artifact_id": "artifact-20260420-a1b2c3d4",
                                "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                                "image_reference": "artifact-20260420-a1b2c3d4",
                            }
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:health-payload"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "pass")
        self.assertEqual(deployment.destination_health.runtime_identity_status, "match")

    def test_generic_web_stable_verification_rejects_mismatched_runtime_identity(
        self,
    ) -> None:
        from control_plane.contracts.runtime_identity import RuntimeIdentity

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                    runtime_identity=RuntimeIdentity(
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        environment_kind="stable",
                        deployment_record_id="deployment-20260420T153000Z-cm-testing",
                        artifact_id="artifact-20260420-a1b2c3d4",
                        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                        image_reference="artifact-20260420-a1b2c3d4",
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "health_payload": {
                            "launchplaneRuntimeIdentity": {
                                "product": "odoo-tenant-cm",
                                "context": "cm",
                                "instance": "testing",
                                "environment_kind": "stable",
                                "deployment_record_id": "deployment-other",
                                "artifact_id": "artifact-20260420-a1b2c3d4",
                                "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                                "image_reference": "artifact-20260420-a1b2c3d4",
                            }
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:identity-mismatch"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_generic_web_stable_verification_rejects_payload_without_expected_runtime_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "health_payload": {
                            "launchplaneRuntimeIdentity": {
                                "product": "odoo-tenant-cm",
                                "context": "cm",
                                "instance": "testing",
                                "environment_kind": "stable",
                                "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                                "artifact_id": "artifact-20260420-a1b2c3d4",
                                "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                                "image_reference": "artifact-20260420-a1b2c3d4",
                            }
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:no-expected-identity"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")
