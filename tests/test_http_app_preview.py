import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from fastapi import FastAPI

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationDestination,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewInventoryItem,
    GenericWebPreviewInventoryResult,
)
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _generic_web_preview_desired_state_identity,
    _generic_web_preview_desired_state_payload,
    _generic_web_preview_desired_state_policy,
    _get_preview_history,
    _get_preview_record,
    _MissingProductReadStore,
    _notification_policy_apply_policy,
    _post_generic_web_preview_desired_state,
    _post_preview_desired_state,
    _post_preview_pr_feedback,
    _preview_desired_state_payload,
    _preview_generation_read_record,
    _preview_pr_feedback_identity,
    _preview_pr_feedback_notification_policy_record,
    _preview_pr_feedback_payload,
    _preview_pr_feedback_policy,
    _preview_read_record,
    _PreviewHistoryProbeStore,
    _PreviewLifecycleCleanupPlanOnlyStore,
    _PreviewLifecycleSweepProfileOnlyStore,
    _PreviewRecordOnlyStore,
    _record_read_policy,
)
from tests.support.http import lifespan_client
from tests.support.auth import _identity, _StubVerifier
from tests.support.profiles import (
    _generic_site_profile_payload,
    _product_profile_payload,
)
from tests.support.stores import (
    _sqlite_database_url,
)


class FastApiPreviewLifecycleCleanupTests(unittest.IsolatedAsyncioTestCase):
    def _cleanup_policy(self, actions: list[str]) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["verireel"],
                        "contexts": ["verireel-testing"],
                        "actions": actions,
                    }
                ]
            }
        )

    def _cleanup_app(
        self, *, root: Path, state_dir: Path, policy: LaunchplaneAuthzPolicy
    ) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(
                _identity(
                    workflow_ref=(
                        "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                )
            ),
            authz_policy=policy,
            control_plane_root_path=root,
            record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
        )

    def _write_cleanup_plan(self, state_dir: Path) -> None:
        FilesystemRecordStore(state_dir=state_dir).write_preview_lifecycle_plan_record(
            PreviewLifecyclePlanRecord(
                plan_id="preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                product="verireel",
                context="verireel-testing",
                planned_at="2026-04-29T19:58:38Z",
                source="preview-janitor",
                status="pass",
                inventory_scan_id="preview-inventory-scan-verireel-testing-20260429T195837Z",
                actual_slugs=("pr-41",),
                orphaned_slugs=("pr-41",),
            )
        )

    async def test_preview_lifecycle_cleanup_records_report_only_cleanup(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            self._write_cleanup_plan(state_dir)
            app = self._cleanup_app(
                root=root,
                state_dir=state_dir,
                policy=self._cleanup_policy(["preview_lifecycle.cleanup"]),
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/previews/lifecycle-cleanup",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                    "source": "preview-janitor",
                    "apply": False,
                },
            )
            payload = response.json()
            cleanup_records = FilesystemRecordStore(
                state_dir=state_dir
            ).list_preview_lifecycle_cleanup_records(context_name="verireel-testing")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            payload["records"]["preview_lifecycle_cleanup_id"], cleanup_records[0].cleanup_id
        )
        self.assertEqual(payload["result"]["status"], "report_only")
        self.assertEqual(payload["result"]["planned_slugs"], ["pr-41"])
        self.assertFalse(cleanup_records[0].apply)

    async def test_preview_lifecycle_cleanup_rejects_missing_plan_without_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            app = self._cleanup_app(
                root=root,
                state_dir=state_dir,
                policy=self._cleanup_policy(["preview_lifecycle.cleanup"]),
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/previews/lifecycle-cleanup",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "plan_id": "preview-lifecycle-plan-verireel-testing-missing",
                    "apply": False,
                },
            )
            cleanup_records = FilesystemRecordStore(
                state_dir=state_dir
            ).list_preview_lifecycle_cleanup_records(context_name="verireel-testing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertEqual(cleanup_records, ())

    async def test_preview_lifecycle_cleanup_replays_idempotency(self) -> None:
        request_payload = {
            "product": "verireel",
            "context": "verireel-testing",
            "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
            "source": "preview-janitor",
            "apply": False,
        }
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            self._write_cleanup_plan(state_dir)
            app = self._cleanup_app(
                root=root,
                state_dir=state_dir,
                policy=self._cleanup_policy(["preview_lifecycle.cleanup"]),
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "preview-lifecycle-cleanup:42",
            }

            async with lifespan_client(app) as client:
                first_response = await _asgi_request(
                    client,
                    "POST",
                    "/v1/previews/lifecycle-cleanup",
                    headers=headers,
                    payload=request_payload,
                )
                replay_response = await _asgi_request(
                    client,
                    "POST",
                    "/v1/previews/lifecycle-cleanup",
                    headers=headers,
                    payload=request_payload,
                )
            cleanup_records = FilesystemRecordStore(
                state_dir=state_dir
            ).list_preview_lifecycle_cleanup_records(context_name="verireel-testing")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(replay_response.json()["result"], first_response.json()["result"])
        self.assertEqual(len(cleanup_records), 1)

    async def test_preview_lifecycle_cleanup_apply_requires_preview_write_store_before_destroy(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            self._write_cleanup_plan(state_dir)
            store = _PreviewLifecycleCleanupPlanOnlyStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=self._cleanup_policy(["preview_lifecycle.cleanup"]),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.workflows.preview_lifecycle_cleanup.execute_verireel_preview_destroy"
            ) as destroy_mock:
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/lifecycle-cleanup",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "product": "verireel",
                        "context": "verireel-testing",
                        "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                        "source": "preview-janitor",
                        "apply": True,
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertIn("preview lifecycle cleanup mutations", response.json()["error"]["message"])
        self.assertEqual(store.cleanup_records, [])
        destroy_mock.assert_not_called()

    async def test_preview_lifecycle_cleanup_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            app = self._cleanup_app(
                root=root,
                state_dir=state_dir,
                policy=self._cleanup_policy(["preview_lifecycle.plan"]),
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/previews/lifecycle-cleanup",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "plan_id": "preview-lifecycle-plan-verireel-testing-20260429T195838Z",
                    "apply": False,
                },
            )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_preview_lifecycle_sweep_uses_enabled_product_profiles(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload(product="syo")
                )
            )
            disabled_payload = _generic_site_profile_payload(product="discord-blue")
            disabled_payload["preview"] = {"enabled": False}
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(disabled_payload)
            )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-syo-syo-pr-41",
                    context="syo",
                    anchor_repo="syo",
                    anchor_pr_number=41,
                    anchor_pr_url="https://github.example/every/syo/pull/41",
                    preview_label="syo/syo#41",
                    canonical_url="https://pr-41.syo.example",
                    state="active",
                    created_at="2026-04-20T10:00:00Z",
                    updated_at="2026-04-20T10:00:00Z",
                    eligible_at="2026-04-20T10:00:00Z",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/launchplane",
                            "workflow_refs": [
                                "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["syo"],
                            "contexts": ["syo"],
                            "actions": ["preview_lifecycle.plan", "preview_lifecycle.cleanup"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    _identity(
                        repository="every/launchplane",
                        workflow_ref=(
                            "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
            )

            with (
                patch(
                    "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory",
                    return_value=GenericWebPreviewInventoryResult(
                        product="syo",
                        context="syo",
                        source="launchplane-preview-lifecycle",
                        app_name_prefix="syo-preview",
                        previews=(
                            GenericWebPreviewInventoryItem(
                                applicationId="app-41",
                                applicationName="syo-preview-pr-41",
                                previewSlug="pr-41",
                            ),
                        ),
                    ),
                ) as inventory_mock,
                patch(
                    "control_plane.preview_lifecycle_cleanup_routes.discover_generic_web_preview_desired_state",
                    return_value=PreviewDesiredStateRecord(
                        desired_state_id="preview-desired-state-syo-20260510T120000Z",
                        product="syo",
                        context="syo",
                        source="launchplane-preview-lifecycle",
                        discovered_at="2026-05-10T12:00:00Z",
                        repository="every/syo",
                        label="preview",
                        anchor_repo="syo",
                        preview_slug_prefix="pr-",
                        status="pass",
                        desired_count=0,
                        desired_previews=(),
                    ),
                ) as desired_mock,
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/lifecycle-sweep",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={"source": "launchplane-preview-lifecycle", "apply": False},
                )
            payload = response.json()
            records = FilesystemRecordStore(state_dir=state_dir)
            plan_records = records.list_preview_lifecycle_plan_records(context_name="syo")
            cleanup_records = records.list_preview_lifecycle_cleanup_records(context_name="syo")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["status"], "pass")
        self.assertEqual(payload["result"]["profile_count"], 1)
        self.assertEqual(payload["result"]["profiles"][0]["product"], "syo")
        self.assertEqual(payload["result"]["profiles"][0]["orphaned_slugs"], ["pr-41"])
        self.assertEqual(plan_records[0].orphaned_slugs, ("pr-41",))
        self.assertEqual(cleanup_records[0].status, "report_only")
        inventory_mock.assert_called_once()
        desired_mock.assert_called_once()

    async def test_preview_lifecycle_sweep_rejects_missing_product_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload(product="syo")
                )
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload(product="verireel")
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/launchplane",
                            "workflow_refs": [
                                "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["syo"],
                            "contexts": ["syo"],
                            "actions": ["preview_lifecycle.plan", "preview_lifecycle.cleanup"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    _identity(
                        repository="every/launchplane",
                        workflow_ref=(
                            "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/previews/lifecycle-sweep",
                headers={"Authorization": "Bearer valid-token"},
                payload={"source": "launchplane-preview-lifecycle"},
            )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(payload["authz"]["request"]["product"], "verireel")
        self.assertEqual(payload["authz"]["request"]["context"], "verireel")
        self.assertEqual(payload["authz"]["request"]["action"], "preview_lifecycle.plan")

    async def test_preview_lifecycle_sweep_requires_write_capable_store_before_drivers(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload(product="syo")
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/launchplane",
                        "workflow_refs": [
                            "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["syo"],
                        "contexts": ["syo"],
                        "actions": ["preview_lifecycle.plan", "preview_lifecycle.cleanup"],
                    }
                ]
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(
                _identity(
                    repository="every/launchplane",
                    workflow_ref=(
                        "every/launchplane/.github/workflows/preview-lifecycle.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                )
            ),
            authz_policy=policy,
            record_store_factory=lambda: _PreviewLifecycleSweepProfileOnlyStore(profile),
        )

        with patch(
            "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory"
        ) as inventory_mock:
            response = await _asgi_request(
                app,
                "POST",
                "/v1/previews/lifecycle-sweep",
                headers={"Authorization": "Bearer valid-token"},
                payload={"source": "launchplane-preview-lifecycle", "apply": True},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertIn("preview lifecycle sweep applies", response.json()["error"]["message"])
        inventory_mock.assert_not_called()


class FastApiPreviewDesiredStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_desired_state_discovers_and_records_labeled_prs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="preview_desired_state.discover",
                    product="verireel",
                    context="verireel-testing",
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            record = PreviewDesiredStateRecord(
                desired_state_id="preview-desired-state-verireel-testing-20260429T213000Z",
                product="verireel",
                context="verireel-testing",
                source="launchplane-preview-lifecycle",
                discovered_at="2026-04-29T21:30:00Z",
                repository="every/verireel",
                label="preview",
                anchor_repo="verireel",
                status="pass",
                desired_count=1,
                desired_previews=(
                    PreviewLifecycleDesiredPreview(
                        preview_slug="pr-42",
                        anchor_repo="verireel",
                        anchor_pr_number=42,
                        anchor_pr_url="https://github.com/every/verireel/pull/42",
                        head_sha="abc1234",
                    ),
                ),
            )

            with patch(
                "control_plane.http_app.discover_github_preview_desired_state",
                return_value=record,
            ) as discover:
                first_response = await _post_preview_desired_state(
                    app,
                    _preview_desired_state_payload(),
                    idempotency_key="preview-desired-state:verireel-testing",
                )
                replay_response = await _post_preview_desired_state(
                    app,
                    _preview_desired_state_payload(),
                    idempotency_key="preview-desired-state:verireel-testing",
                )
            records = store.list_preview_desired_state_records(context_name="verireel-testing")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        payload = first_response.json()
        self.assertEqual(
            payload["records"]["preview_desired_state_id"],
            "preview-desired-state-verireel-testing-20260429T213000Z",
        )
        self.assertEqual(payload["result"]["desired_previews"][0]["preview_slug"], "pr-42")
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(records), 1)
        discover.assert_called_once()

    async def test_preview_desired_state_rejects_unauthorized_workflow(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="preview_lifecycle.plan",
                product="verireel",
                context="verireel-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_desired_state(app, _preview_desired_state_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_generic_web_preview_desired_state_uses_profile_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_generic_web_preview_desired_state_identity()),
                authz_policy=_generic_web_preview_desired_state_policy(
                    context="sellyouroutboard-testing"
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            record = PreviewDesiredStateRecord(
                desired_state_id="preview-desired-state-syo-testing-1",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                source="generic-web-preview",
                discovered_at="2026-04-30T21:00:00Z",
                repository="cbusillo/sellyouroutboard",
                label="preview",
                anchor_repo="sellyouroutboard",
                status="pass",
                desired_count=0,
            )

            with patch(
                "control_plane.http_app.discover_generic_web_preview_desired_state",
                return_value=record,
            ) as discover:
                response = await _post_generic_web_preview_desired_state(
                    app,
                    _generic_web_preview_desired_state_payload(),
                    idempotency_key="generic-web-preview-desired-state:syo",
                )
            records = store.list_preview_desired_state_records(
                context_name="sellyouroutboard-testing"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["records"]["preview_desired_state_id"],
            "preview-desired-state-syo-testing-1",
        )
        discover.assert_called_once()
        _, kwargs = discover.call_args
        self.assertEqual(kwargs["profile"].preview.context, "sellyouroutboard-testing")
        self.assertEqual(records[0].desired_state_id, "preview-desired-state-syo-testing-1")

    async def test_generic_web_preview_desired_state_rejects_wrong_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_generic_web_preview_desired_state_identity()),
                authz_policy=_generic_web_preview_desired_state_policy(context="different-context"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_generic_web_preview_desired_state(
                app,
                _generic_web_preview_desired_state_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_generic_web_preview_desired_state_keeps_profile_errors_invalid_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            profile_payload = _product_profile_payload()
            profile_payload["preview"] = {
                "enabled": False,
                "context": "sellyouroutboard-testing",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_generic_web_preview_desired_state_identity()),
                authz_policy=_generic_web_preview_desired_state_policy(
                    context="sellyouroutboard-testing"
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.discover_generic_web_preview_desired_state"
            ) as discover:
                response = await _post_generic_web_preview_desired_state(
                    app,
                    _generic_web_preview_desired_state_payload(),
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        discover.assert_not_called()

    async def test_openapi_includes_preview_desired_state_routes(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        self.assertEqual(
            openapi["paths"]["/v1/previews/desired-state"]["post"]["operationId"],
            "apply_preview_desired_state",
        )
        self.assertEqual(
            openapi["paths"]["/v1/drivers/generic-web/preview-desired-state"]["post"][
                "operationId"
            ],
            "apply_generic_web_preview_desired_state",
        )


class FastApiPreviewPrFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_pr_feedback_records_skipped_delivery_without_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["preview_pr_feedback_id"], feedback_records[0].feedback_id
        )
        self.assertEqual(payload["result"]["delivery_status"], "skipped")
        self.assertIn("Launchplane preview is ready", payload["result"]["comment_markdown"])
        self.assertIn("GITHUB_TOKEN", feedback_records[0].error_message)

    async def test_preview_pr_feedback_derives_context_from_product_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(
                app,
                _preview_pr_feedback_payload(context=None),
            )
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["context"], "verireel-testing")
        self.assertEqual(feedback_records[0].context, "verireel-testing")

    async def test_preview_pr_feedback_rejects_context_mismatch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(
                app,
                _preview_pr_feedback_payload(context="wrong-context"),
            )
            feedback_records = store.list_preview_pr_feedback_records()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(feedback_records, ())

    async def test_preview_pr_feedback_context_derivation_requires_product_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(
                app,
                _preview_pr_feedback_payload(context=None),
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_preview_pr_feedback_context_derivation_requires_profile_store(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_pr_feedback_identity()),
            authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_pr_feedback(
            app,
            _preview_pr_feedback_payload(context=None),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_preview_pr_feedback_context_derivation_requires_enabled_preview(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            product_profile = _product_profile_payload("verireel")
            product_profile["preview"] = {
                "enabled": False,
                "context": "verireel-testing",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(product_profile)
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(
                app,
                _preview_pr_feedback_payload(context=None),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_preview_pr_feedback_replays_idempotent_notification(self) -> None:
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ),
        ):
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
                preview_pr_feedback_discord_sender=send_discord,
            )
            try:
                secret_result = control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration="preview-pr-feedback-notifications",
                    name="discord webhook",
                    plaintext_value="https://discord.com/api/webhooks/test/webhook",
                    binding_key="DISCORD_WEBHOOK",
                    context_name="launchplane",
                    instance_name="preview-feedback",
                    actor="test",
                    source_label="test",
                )
                store.write_preview_pr_feedback_notification_policy_record(
                    _preview_pr_feedback_notification_policy_record(
                        policy_id="preview-pr-feedback-notification-discord",
                        product="verireel",
                        context="verireel-testing",
                        repository="every/verireel",
                    ).model_copy(
                        update={
                            "destinations": (
                                PreviewPrFeedbackNotificationDestination(
                                    destination_id="discord",
                                    kind="discord",
                                    discord_webhook_secret=str(secret_result["secret_id"]),
                                ),
                            )
                        }
                    )
                )
                first_response = await _post_preview_pr_feedback(
                    app,
                    _preview_pr_feedback_payload(),
                    idempotency_key="preview-pr-feedback:verireel:42",
                )
                replay_response = await _post_preview_pr_feedback(
                    app,
                    _preview_pr_feedback_payload(),
                    idempotency_key="preview-pr-feedback:verireel:42",
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(sent_payloads), 1)
        first_payload = first_response.json()
        self.assertEqual(
            first_payload["result"]["notifications"][0]["delivery_status"], "delivered"
        )
        sent_embeds = cast(list[dict[str, object]], sent_payloads[0][1]["embeds"])
        sent_embed = sent_embeds[0]
        sent_embed_fields = cast(list[dict[str, str]], sent_embed["fields"])
        sent_fields = {field["name"]: field["value"] for field in sent_embed_fields}
        self.assertEqual(sent_fields["Pull request"], "https://github.com/every/verireel/pull/42")
        self.assertEqual(
            sent_fields["Workflow"], "https://github.com/every/verireel/actions/runs/123"
        )

    async def test_preview_pr_feedback_dry_run_authorizes_without_writing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(
                app,
                _preview_pr_feedback_payload(context=None, dry_run=True),
            )
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["preview_pr_feedback"], "authorized")
        self.assertEqual(response.json()["result"]["context"], "verireel-testing")
        self.assertEqual(feedback_records, ())

    async def test_preview_pr_feedback_accepts_lifecycle_refresh_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_refresh.execute"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(feedback_records[0].status, "ready")

    async def test_preview_pr_feedback_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload("verireel"))
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_generation.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_openapi_includes_preview_pr_feedback_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/previews/pr-feedback"]["post"]
        self.assertEqual(route["operationId"], "apply_preview_pr_feedback")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )


class FastApiPreviewReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_read_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_preview_record(_preview_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["record"]["preview_id"], "preview-example-site-example-site-pr-42")
        self.assertEqual(payload["record"]["context"], "example-site")
        self.assertEqual(payload["record"]["state"], "active")

    async def test_preview_history_returns_preview_and_generations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            preview = _preview_read_record()
            store.write_preview_record(preview)
            store.write_preview_generation_record(
                _preview_generation_read_record(sequence=1, state="ready")
            )
            store.write_preview_generation_record(
                _preview_generation_read_record(sequence=2, state="verifying")
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_history(app, preview.preview_id)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["preview"]["preview_id"], "preview-example-site-example-site-pr-42"
        )
        self.assertNotIn("record", payload)
        self.assertEqual(
            [generation["sequence"] for generation in payload["generations"]],
            [2, 1],
        )
        self.assertEqual(payload["generations"][0]["state"], "verifying")

    async def test_preview_read_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_preview_record(
            app,
            "preview-example-site-example-site-pr-42",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_preview_read_rejects_wrong_context_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_preview_record(_preview_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="other-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_preview_history_rejects_wrong_context_before_listing_generations(
        self,
    ) -> None:
        store = _PreviewHistoryProbeStore(_preview_read_record())
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="preview.read",
                context="other-site",
            ),
            record_store_factory=lambda: store,
        )

        response = await _get_preview_history(
            app,
            "preview-example-site-example-site-pr-42",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(store.list_preview_generation_calls, 0)

    async def test_preview_read_returns_not_found_for_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_preview_read_requires_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_preview_record", payload["error"]["message"])

    async def test_preview_history_requires_history_capable_store(self) -> None:
        store = _PreviewRecordOnlyStore(_preview_read_record())
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _get_preview_history(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("list_preview_generation_records", payload["error"]["message"])

    async def test_openapi_includes_preview_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        preview_route = openapi["paths"]["/v1/previews/{preview_id}"]["get"]
        history_route = openapi["paths"]["/v1/previews/{preview_id}/history"]["get"]
        self.assertEqual(preview_route["operationId"], "read_preview_record")
        self.assertEqual(history_route["operationId"], "read_preview_history")
        self.assertEqual(
            preview_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PreviewRecordResponse",
        )
        self.assertEqual(
            history_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PreviewHistoryResponse",
        )
        for route in (preview_route, history_route):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
            self.assertIn("401", route["responses"])
            self.assertIn("403", route["responses"])
            self.assertIn("404", route["responses"])
            self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["PreviewRecordResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["PreviewHistoryResponse"]["additionalProperties"],
            False,
        )
