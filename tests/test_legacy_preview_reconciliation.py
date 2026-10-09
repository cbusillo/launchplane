import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from control_plane.contracts.preview_record import PreviewRecord
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
        self.search.side_effect = ValueError("incomplete enumeration")
        response = await self.apply(plan)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read_preview_record(self.preview.preview_id), self.preview)
