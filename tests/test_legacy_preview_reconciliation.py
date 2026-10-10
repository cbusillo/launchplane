import unittest
import asyncio
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane import http_app as http_app_module
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.legacy_preview_reconciliation import (
    LEGACY_PREVIEW_RECONCILIATION_ROUTE as ROUTE,
    LegacyPreviewReconciliationRequest,
    bind_legacy_preview,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewInventoryResult,
    GenericWebPreviewDestroyResult,
)
from control_plane.workflows.preview_lifecycle_cleanup import build_preview_lifecycle_cleanup_record
from tests.http_app_test_support import _local_operator_bearer_config
from tests.support.auth import _identity, _local_operator_policy, _StubVerifier
from tests.support.http import request
from tests.support.profiles import _product_profile_payload


class LegacyPreviewReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.root / 'state.db'}"

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        payload = _product_profile_payload(product="example-site")
        payload["preview"] = {
            "enabled": True,
            "context": "example-site-preview",
            "slug_template": "pr-{number}",
        }
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.preview = PreviewRecord(
            preview_id="preview-legacy-1",
            context=self.profile.preview.context,
            anchor_repo="example-site",
            anchor_pr_number=1,
            anchor_pr_url="https://github.com/cbusillo/example-site/pull/1",
            preview_label="preview",
            canonical_url="https://pr-1.example.test",
            state="active",
            created_at=self.profile.updated_at,
            updated_at=self.profile.updated_at,
            eligible_at=self.profile.updated_at,
        )
        self.store.write_preview_record(self.preview)
        self.app = self.build_app(
            ("preview_inventory.read", "preview_destroy.execute", "preview_destroyed.write")
        )
        self.config = patch(
            "control_plane.legacy_preview_reconciliation.dokploy_source.read_dokploy_config",
            return_value=("https://provider.invalid", "fixture"),
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        self.inventory = patch(
            "control_plane.legacy_preview_reconciliation.dokploy_api.search_dokploy_applications",
            return_value=(),
        )
        self.search = self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def build_app(self, actions: tuple[str, ...]) -> Any:
        policy = _local_operator_policy(
            actions=actions,
            products=(self.profile.product,),
            contexts=(self.profile.preview.context,),
        )
        if not self.store.database_url.startswith("sqlite"):
            records = self.store.list_authz_policy_records(status="active")
            if records:
                current = records[0]
                replacement = LaunchplaneAuthzPolicyRecord(
                    record_id=f"fixture-policy-{current.revision + 1}",
                    revision=current.revision + 1,
                    source="test",
                    updated_at=self.profile.updated_at,
                    policy=policy,
                )
                result = self.store.compare_and_write_authz_policy_record(
                    expected_record=current, replacement_record=replacement
                )
                self.assertEqual(result.status, "written")
            else:
                self.store.seed_authz_policy_if_absent(
                    LaunchplaneAuthzPolicyRecord(
                        record_id="fixture-policy",
                        source="test",
                        updated_at=self.profile.updated_at,
                        policy=policy,
                    )
                )
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            control_plane_root_path=self.root,
            record_store_factory=lambda: self.store,
        )

    async def call(self, mode: str, *, key: str = "", app: Any = None, **extra: object) -> Any:
        headers = {"Authorization": "Bearer local-operator-token"}
        if key:
            headers["Idempotency-Key"] = key
        return await request(
            self.app if app is None else app,
            "POST",
            ROUTE,
            headers=headers,
            payload={
                "product": self.profile.product,
                "preview_id": self.preview.preview_id,
                "mode": mode,
                "reason": "Reconcile provider-absent history",
                **extra,
            },
        )

    async def plan(self) -> dict[str, Any]:
        response = await self.call("plan", key="review-plan")
        self.assertEqual(response.status_code, 202, response.text)
        return dict(response.json()["result"])

    async def apply(self, plan: dict[str, Any], **extra: object) -> Any:
        return await self.call(
            "apply",
            key="apply-plan",
            reviewed_plan=True,
            plan_idempotency_key="review-plan",
            expected_plan_digest=plan["plan_digest"],
            **extra,
        )

    async def test_inspection_plan_apply_and_independent_readback(self) -> None:
        inspect = await self.call("inspect")
        self.assertEqual(inspect.status_code, 202, inspect.text)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)
        self.assertEqual(self.store.list_preview_inventory_scan_records(), ())
        plan = await self.plan()
        self.assertTrue(plan["apply_eligible"])
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)
        applied = await self.apply(plan)
        self.assertEqual(applied.status_code, 202, applied.text)
        closed = self.store.read_preview_record(self.preview.preview_id)
        self.assertEqual(closed.state, "destroyed")
        self.assertTrue(closed.destroyed_at)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        readback = await self.call("inspect")
        self.assertEqual(readback.json()["result"]["preview_state"], "destroyed")
        self.assertTrue(readback.json()["result"]["provider_absence_verified"])
        self.assertFalse(readback.json()["result"]["apply_eligible"])
        replay = await self.apply(plan)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])

    async def test_read_grant_does_not_authorize_apply(self) -> None:
        plan = await self.plan()
        readonly = self.build_app(("preview_inventory.read",))
        response = await self.apply(plan, app=readonly)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_apply_requires_saved_plan_for_same_caller_and_preview(self) -> None:
        response = await self.call(
            "apply",
            key="apply-plan",
            reviewed_plan=True,
            plan_idempotency_key="missing-plan",
            expected_plan_digest="a" * 64,
        )
        self.assertEqual(response.status_code, 409)
        self.search.assert_not_called()
        plan = await self.plan()
        response = await self.apply(plan, preview_id="missing-preview")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_changed_preview_refuses_reviewed_apply(self) -> None:
        plan = await self.plan()
        changed = self.preview.model_copy(update={"state": "paused"})
        self.store.write_preview_record(changed)
        response = await self.apply(plan)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), changed)

    async def test_historical_generations_survive_reconciliation(self) -> None:
        generation = PreviewGenerationRecord(
            generation_id="historical-generation",
            preview_id=self.preview.preview_id,
            sequence=1,
            state="ready",
            requested_reason="historical",
            requested_at=self.preview.created_at,
            finished_at=self.preview.created_at,
            resolved_manifest_fingerprint="historical-manifest",
            anchor_summary=PreviewPullRequestSummary.model_validate(
                {
                    "repo": self.profile.repository,
                    "pr_number": 1,
                    "head_sha": "a" * 40,
                    "pr_url": self.preview.anchor_pr_url,
                }
            ),
        )
        self.store.write_preview_generation_record(generation)
        self.store.write_preview_record(
            self.preview.model_copy(
                update={
                    "active_generation_id": generation.generation_id,
                    "serving_generation_id": generation.generation_id,
                    "latest_generation_id": generation.generation_id,
                }
            )
        )
        plan = await self.plan()
        applied = await self.apply(plan)
        self.assertEqual(applied.status_code, 202, applied.text)
        closed = self.store.read_preview_record(self.preview.preview_id)
        self.assertEqual(closed.state, "destroyed")
        self.assertEqual(closed.active_generation_id, "")
        self.assertEqual(closed.serving_generation_id, "")
        self.assertEqual(closed.latest_generation_id, generation.generation_id)
        self.assertEqual(
            self.store.read_preview_generation_record(generation.generation_id), generation
        )

    async def test_authority_change_at_atomic_commit_refuses_apply(self) -> None:
        plan = await self.plan()
        commit = self.store.commit_legacy_preview_reconciliation

        def changed_commit(**kwargs: Any) -> None:
            self.store.write_preview_record(self.preview.model_copy(update={"state": "paused"}))
            commit(**kwargs)

        with patch.object(
            self.store, "commit_legacy_preview_reconciliation", side_effect=changed_commit
        ):
            response = await self.apply(plan)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id).state, "paused")

    async def test_changed_idempotency_request_does_not_replay_success(self) -> None:
        plan = await self.plan()
        applied = await self.apply(plan)
        self.assertEqual(applied.status_code, 202)
        conflict = await self.apply(plan, reason="another intent")
        self.assertEqual(conflict.status_code, 409)

    async def test_shared_context_and_wrong_product_fail_closed(self) -> None:
        other = self.profile.model_copy(
            update={"product": "other-site", "repository": "cbusillo/other-site"}
        )
        self.store.write_product_profile_record(other)
        response = await self.call("plan", key="shared-plan")
        self.assertEqual(response.status_code, 409)
        with self.assertRaises(ValueError):
            bind_legacy_preview(
                self.store,
                LegacyPreviewReconciliationRequest(
                    product=other.product, preview_id=self.preview.preview_id, reason="fixture"
                ),
            )

    async def test_missing_generation_pointer_is_not_provider_absence(self) -> None:
        self.store.write_preview_record(
            self.preview.model_copy(update={"latest_generation_id": "missing-generation"})
        )
        response = await self.call("plan", key="missing-evidence")
        self.assertEqual(response.status_code, 409)
        self.search.assert_not_called()

    async def test_provider_presence_blocks_plan_without_delete(self) -> None:
        bound = bind_legacy_preview(
            self.store,
            LegacyPreviewReconciliationRequest(
                product=self.profile.product, preview_id=self.preview.preview_id, reason="fixture"
            ),
        )
        item = {"applicationId": "provider-app"}
        self.search.return_value = (item,)
        with (
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    **item,
                    "name": bound.application_name,
                    "appName": "example-site-pr-1-suffix",
                },
            ),
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                return_value=(),
            ),
            patch("control_plane.dokploy.api.dokploy_request") as provider_write,
        ):
            plan = await self.plan()
            self.assertEqual(plan["provider_state"], "present")
            self.assertFalse(plan["apply_eligible"])
            response = await self.apply(plan)
            self.assertEqual(response.status_code, 409)
            provider_write.assert_not_called()

    async def test_renamed_orphan_and_incomplete_provider_payload_refuse_absence(self) -> None:
        self.search.return_value = ({"applicationId": "orphan"},)
        for app in (
            {
                "applicationId": "orphan",
                "name": "renamed",
                "appName": "renamed",
                "dockerImage": self.profile.image.repository + ":old",
            },
            {"applicationId": "orphan", "name": "missing-app-name"},
        ):
            with (
                self.subTest(app=app),
                patch(
                    "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                    return_value=app,
                ),
                patch(
                    "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                    return_value=(),
                ),
            ):
                response = await self.call("plan", key="orphan-plan")
                self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_provider_appears_between_plan_and_apply(self) -> None:
        plan = await self.plan()
        self.search.return_value = ({"applicationId": "new-preview"},)
        with (
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "new-preview",
                    "name": "unfamiliar",
                    "appName": "unfamiliar",
                },
            ),
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                return_value=({"host": "pr-1.example.test"},),
            ),
        ):
            response = await self.apply(plan)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_incomplete_enumeration_refuses_apply(self) -> None:
        plan = await self.plan()
        self.search.side_effect = ValueError("incomplete enumeration")
        response = await self.apply(plan)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)

    async def test_unrelated_deploy_and_profile_do_not_invalidate_review(self) -> None:
        self.search.return_value = ({"applicationId": "other-app"},)
        app = {
            "applicationId": "other-app",
            "name": "other-site",
            "appName": "other-site",
            "dockerImage": "other-image:old",
        }
        with (
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                side_effect=lambda **_: app,
            ),
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                return_value=(),
            ),
        ):
            plan = await self.plan()
            app["dockerImage"] = "other-image:new"
            other_payload = _product_profile_payload(product="other-site")
            self.store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(other_payload)
            )
            response = await self.apply(plan)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id).state, "destroyed")

    async def test_tracked_sibling_preview_does_not_block_absent_legacy_preview(self) -> None:
        sibling = self.preview.model_copy(
            update={
                "preview_id": "preview-sibling-7",
                "anchor_pr_number": 7,
                "canonical_url": "https://pr-7.example.test",
            }
        )
        self.store.write_preview_record(sibling)
        target = DokployTargetIdRecord(
            context=sibling.context,
            instance="pr-7",
            target_id="sibling-app",
            updated_at=sibling.updated_at,
        )
        self.store.write_dokploy_target_id_record(target)
        self.search.return_value = ({"applicationId": target.target_id},)
        with (
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": target.target_id,
                    "name": "example-site-preview-pr-7",
                    "appName": "example-site-pr-7-suffix",
                    "dockerImage": self.profile.image.repository + ":current",
                },
            ),
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                return_value=({"host": "pr-7.example.test"},),
            ) as domains,
        ):
            plan = await self.plan()
            domains.return_value = ({"host": "pr-1.example.test"},)
            blocked = await self.apply(plan)
            self.assertEqual(blocked.status_code, 409)
            domains.return_value = ({"host": "pr-7.example.test"},)
            applied = await self.apply(plan)
            self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(self.store.read_preview_record(sibling.preview_id), sibling)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=sibling.context, instance_name="pr-7"
            ),
            target,
        )

    async def test_shared_sibling_target_refuses_absence(self) -> None:
        sibling = self.preview.model_copy(update={"preview_id": "sibling", "anchor_pr_number": 7})
        self.store.write_preview_record(sibling)
        for context, instance in ((sibling.context, "pr-7"), ("other-context", "testing")):
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=context,
                    instance=instance,
                    target_id="shared-app",
                    updated_at=sibling.updated_at,
                )
            )
        response = await self.call("plan", key="shared-sibling")
        self.assertEqual(response.status_code, 409)
        self.search.assert_not_called()

    async def test_mixed_case_gitea_orphan_blocks_absence(self) -> None:
        self.search.return_value = ({"applicationId": "orphan"},)
        with (
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "orphan",
                    "name": "renamed",
                    "appName": "renamed",
                    "giteaRepository": "https://git.example/Cbusillo/Example-Site.git",
                },
            ),
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_application_domains",
                return_value=(),
            ),
        ):
            response = await self.call("plan", key="mixed-case")
        self.assertEqual(response.status_code, 409)

    async def test_malformed_domain_inventory_is_not_absence(self) -> None:
        self.search.return_value = ({"applicationId": "other-app"},)
        with (
            patch(
                "control_plane.legacy_preview_reconciliation.dokploy_api.fetch_dokploy_target_payload",
                return_value={"applicationId": "other-app", "name": "other", "appName": "other"},
            ),
            patch("control_plane.dokploy.api.dokploy_request", return_value=["malformed domain"]),
        ):
            response = await self.call("plan", key="malformed-domain")
        self.assertEqual(response.status_code, 409)

    async def test_slow_provider_scan_keeps_http_event_loop_available(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_scan(**_: object) -> tuple[()]:
            started.set()
            if not release.wait(5):
                raise ValueError("fixture timed out")
            return ()

        self.search.side_effect = slow_scan
        task = asyncio.create_task(self.call("inspect"))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            healthy = await asyncio.wait_for(request(self.app, "GET", "/v1/health"), 2)
            self.assertEqual(healthy.status_code, 200)
        finally:
            release.set()
        inspected = await asyncio.wait_for(task, 5)
        self.assertEqual(inspected.status_code, 202, inspected.text)

    async def test_unknown_product_has_bounded_not_found_result(self) -> None:
        response = await self.call("inspect", product="missing-product")
        self.assertEqual(response.status_code, 404)
        self.search.assert_not_called()

    async def test_slow_preview_destroy_keeps_service_available_and_preserves_cancelled_receipt(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_destroy(**_: object) -> tuple[dict[str, str], dict[str, object]]:
            started.set()
            if not release.wait(5):
                raise ValueError("fixture timed out")
            return {}, {"destroy_status": "pass", "destroy_outcome": "destroyed"}

        async def destroy() -> Any:
            return await request(
                self.app,
                "POST",
                "/v1/drivers/generic-web/preview-destroy",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "slow-destroy",
                },
                payload={
                    "product": self.profile.product,
                    "destroy": {
                        "product": self.profile.product,
                        "anchor_pr_number": 1,
                        "destroy_reason": "fixture",
                    },
                },
            )

        with patch(
            "control_plane.http_routes.generic_web.apply_generic_web_preview_destroy_result",
            side_effect=slow_destroy,
        ) as driver:
            task = asyncio.create_task(destroy())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                health = await asyncio.wait_for(request(self.app, "GET", "/v1/health"), 2)
                self.assertEqual(health.status_code, 200)
                duplicate = await asyncio.wait_for(destroy(), 2)
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
                task.cancel()
                await asyncio.sleep(0)
                duplicate = await asyncio.wait_for(destroy(), 2)
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            replay = await destroy()
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertTrue(replay.json()["replayed"])
            driver.assert_called_once()

    async def test_lifecycle_completion_keeps_service_available_and_scope_does_not_expand(
        self,
    ) -> None:
        self.app = self.build_app(
            ("preview_inventory.read", "preview_lifecycle.plan", "preview_lifecycle.cleanup")
        )
        plan = PreviewLifecyclePlanRecord(
            plan_id="lifecycle-plan",
            product=self.profile.product,
            context=self.profile.preview.context,
            planned_at=self.preview.created_at,
            source="fixture",
            status="pass",
            inventory_scan_id="fixture-inventory",
            actual_slugs=("pr-1",),
            orphaned_slugs=("pr-1",),
        )
        self.store.write_preview_lifecycle_plan_record(plan)
        for operation in ("cleanup", "sweep"):
            with self.subTest(operation=operation):
                started = threading.Event()
                release = threading.Event()
                name = (
                    "build_preview_lifecycle_cleanup_record"
                    if operation == "cleanup"
                    else "build_preview_lifecycle_sweep"
                )
                original = getattr(http_app_module, name)

                def slow_work(**kwargs: Any) -> Any:
                    started.set()
                    if not release.wait(5):
                        raise ValueError("fixture timed out")
                    return original(**kwargs)

                payload: dict[str, object] = {"source": "fixture", "apply": False}
                if operation == "cleanup":
                    payload.update(
                        product=self.profile.product,
                        context=self.profile.preview.context,
                        plan_id=plan.plan_id,
                    )

                async def call() -> Any:
                    return await request(
                        self.app,
                        "POST",
                        f"/v1/previews/lifecycle-{operation}",
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": f"slow-lifecycle-{operation}",
                        },
                        payload=payload,
                    )

                with (
                    patch.object(http_app_module, name, side_effect=slow_work) as work,
                    patch(
                        "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory",
                        return_value=GenericWebPreviewInventoryResult(
                            product=self.profile.product,
                            context=self.profile.preview.context,
                            source="fixture",
                            app_name_prefix="fixture-preview",
                            previews=(),
                        ),
                    ),
                    patch(
                        "control_plane.preview_lifecycle_cleanup_routes.discover_generic_web_preview_desired_state",
                        return_value=PreviewDesiredStateRecord(
                            desired_state_id="fixture-desired",
                            product=self.profile.product,
                            context=self.profile.preview.context,
                            source="fixture",
                            discovered_at=self.preview.created_at,
                            repository=self.profile.repository,
                            anchor_repo=self.preview.anchor_repo,
                            status="pass",
                            desired_count=0,
                        ),
                    ),
                ):
                    task = asyncio.create_task(call())
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 3))
                        concurrent = await call()
                        self.assertEqual(concurrent.status_code, 409, concurrent.text)
                        health = await asyncio.wait_for(request(self.app, "GET", "/v1/health"), 2)
                        self.assertEqual(health.status_code, 200)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        still_running = await call()
                        self.assertEqual(still_running.status_code, 409, still_running.text)
                        if operation == "sweep":
                            other = self.profile.model_copy(
                                update={
                                    "product": "later-site",
                                    "repository": "cbusillo/later-site",
                                    "preview": self.profile.preview.model_copy(
                                        update={"context": "later-preview"}
                                    ),
                                }
                            )
                            self.store.write_product_profile_record(other)
                    finally:
                        release.set()

                    async def wait_for_completion() -> Any:
                        while True:
                            completed = await call()
                            if completed.status_code != 409:
                                return completed
                            await asyncio.sleep(0.01)

                    response = await asyncio.wait_for(wait_for_completion(), 5)
                    self.assertEqual(response.status_code, 202, response.text)
                    self.assertTrue(response.json()["replayed"])
                    if operation == "sweep":
                        self.assertEqual(
                            [entry["product"] for entry in response.json()["result"]["profiles"]],
                            [self.profile.product],
                        )
                        self.assertEqual(
                            self.store.list_preview_lifecycle_plan_records(
                                context_name="later-preview"
                            ),
                            (),
                        )
                    replay = await call()
                    self.assertEqual(replay.status_code, 202, replay.text)
                    self.assertTrue(replay.json()["replayed"])
                    work.assert_called_once()

    async def test_cleanup_destroy_keeps_checked_profile_and_refuses_wrong_context(self) -> None:
        self.app = self.build_app(("preview_lifecycle.cleanup",))
        plan = PreviewLifecyclePlanRecord(
            plan_id="profile-bound-cleanup",
            product=self.profile.product,
            context=self.profile.preview.context,
            planned_at=self.preview.created_at,
            source="fixture",
            status="pass",
            inventory_scan_id="fixture-inventory",
            orphaned_slugs=("pr-1",),
        )
        self.store.write_preview_lifecycle_plan_record(plan)
        started = threading.Event()
        release = threading.Event()
        original = build_preview_lifecycle_cleanup_record

        def slow_work(**kwargs: Any) -> Any:
            started.set()
            if not release.wait(5):
                raise ValueError("fixture timed out")
            return original(**kwargs)

        async def call(key: str) -> Any:
            return await request(
                self.app,
                "POST",
                "/v1/previews/lifecycle-cleanup",
                headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
                payload={
                    "product": self.profile.product,
                    "context": self.profile.preview.context,
                    "plan_id": plan.plan_id,
                    "source": "fixture",
                    "apply": True,
                },
            )

        result = GenericWebPreviewDestroyResult(
            destroy_status="pass",
            destroy_started_at=self.preview.created_at,
            destroy_finished_at=self.preview.created_at,
            product=self.profile.product,
            context=self.profile.preview.context,
            preview_slug="pr-1",
            application_name="fixture-preview-pr-1",
            application_id="fixture-app",
        )
        changed = self.profile.model_copy(
            update={
                "preview": self.profile.preview.model_copy(update={"context": "changed-preview"})
            }
        )
        with (
            patch(
                "control_plane.http_app.build_preview_lifecycle_cleanup_record",
                side_effect=slow_work,
            ),
            patch(
                "control_plane.workflows.generic_web_preview._execute_generic_web_preview_destroy_unserialized",
                return_value=result,
            ) as provider,
        ):
            task = asyncio.create_task(call("checked-cleanup"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                self.store.write_product_profile_record(changed)
            finally:
                release.set()
            response = await asyncio.wait_for(task, 5)
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(provider.call_args.kwargs["profile"], self.profile)
            self.assertEqual(
                self.store.read_preview_record(self.preview.preview_id).state, "destroyed"
            )
        with patch(
            "control_plane.workflows.generic_web_preview._execute_generic_web_preview_destroy_unserialized"
        ) as provider:
            response = await call("wrong-context-cleanup")
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["result"]["status"], "blocked")
            provider.assert_not_called()
            disabled = self.profile.model_copy(
                update={"preview": self.profile.preview.model_copy(update={"enabled": False})}
            )
            self.store.write_product_profile_record(disabled)
            response = await call("disabled-preview-cleanup")
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["result"]["status"], "blocked")
            provider.assert_not_called()
