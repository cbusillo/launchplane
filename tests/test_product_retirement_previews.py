"""Integrated retirement through the browser admin HTTP boundary and real storage."""

import unittest
from typing import Any, cast
from control_plane.dokploy import api as dokploy_api
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, GitHubHumanPolicyRule
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _github_human_identity,
    _github_oauth_config,
    _browser_mutation_headers,
    _RejectingVerifier,
)
from tests.support.http import request
from tests.support.profiles import _generic_site_profile_payload
from control_plane.product_reconcile import request_product_reconcile_sweep


class IntegratedProductRetirementTests(unittest.IsolatedAsyncioTestCase):
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.root / 'state.db'}"

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        payload = _generic_site_profile_payload()
        payload["lanes"] = [
            lane
            for lane in cast(list[dict[str, Any]], payload["lanes"])
            if lane["instance"] == "testing"
        ]
        payload["production_use"] = "prelaunch"
        payload["preview"] = {
            "enabled": True,
            "context": "example-site-preview",
            "slug_template": "pr-{number}",
        }
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        context = self.profile.lanes[0].context
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=context,
                instance="testing",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                target_id="testing-app",
                display_name="example-site-testing",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=context,
                instance="testing",
                target_type="application",
                target_name="example-site-testing",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=context,
                instance="testing",
                target_id="testing-app",
                updated_at=self.profile.updated_at,
            )
        )
        for number in (1, 6):
            self.store.write_preview_record(
                PreviewRecord(
                    preview_id=f"legacy-{number}",
                    context=self.profile.preview.context,
                    anchor_repo="example-site",
                    anchor_pr_number=number,
                    anchor_pr_url=f"https://github.com/every/example-site/pull/{number}",
                    preview_label="preview",
                    canonical_url=f"https://pr-{number}.example.invalid",
                    state="active",
                    created_at=self.profile.updated_at,
                    updated_at=self.profile.updated_at,
                    eligible_at=self.profile.updated_at,
                )
            )
        self.apps = {
            "testing-app": {
                "applicationId": "testing-app",
                "name": "example-site-testing",
                "appName": "example-site-testing-abc",
                "applicationStatus": "idle",
            },
            "preview-6": {
                "applicationId": "preview-6",
                "name": "example-site-pr-6",
                "appName": "example-site-pr-6-abc",
                "applicationStatus": "idle",
            },
        }
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_humans=(
                GitHubHumanPolicyRule(
                    github_ids=(123,),
                    roles=("admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=("authz_policy_grant.write",),
                ),
            ),
        )
        if not self.store.database_url.startswith("sqlite"):
            from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord

            self.store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="fixture-retirement-admin",
                    source="test",
                    updated_at=self.profile.updated_at,
                    policy=policy,
                )
            )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            control_plane_root_path=self.root,
        )
        self.payload = {
            "mode": "plan",
            "include_previews": True,
            "product": self.profile.product,
            "instance": "testing",
            "reason": "Retire the obsolete product.",
            "related_issue": "example/launchplane#1",
        }
        for app in self.apps.values():
            app["dockerImage"] = self.profile.image.repository + ":fixture"
        self.deleted: list[str] = []
        for target, replacement in (
            (
                "control_plane.product_retirement.dokploy_source.read_dokploy_config",
                lambda **_: ("https://provider.invalid", "fixture"),
            ),
            (
                "control_plane.legacy_preview_reconciliation.dokploy_api.search_dokploy_applications",
                lambda **_: tuple(dict(app) for app in self.apps.values()),
            ),
            (
                "control_plane.product_retirement.dokploy_api.fetch_dokploy_target_payload",
                self.fetch,
            ),
            (
                "control_plane.product_retirement.dokploy_api.fetch_dokploy_application_domains",
                lambda **_: (),
            ),
            (
                "control_plane.product_retirement.dokploy_api.deployment_history_for_target",
                lambda **_: dokploy_api.DeploymentHistory(
                    state="present", latest_deployment={"status": "done"}
                ),
            ),
            (
                "control_plane.product_retirement.dokploy_api.delete_dokploy_application",
                self.delete,
            ),
            (
                "control_plane.workflows.preview_resource_destroy._preview_resource_domain_ids",
                lambda **_: (),
            ),
            (
                "control_plane.workflows.preview_resource_destroy._delete_preview_resource",
                self.delete_resource,
            ),
        ):
            mock = patch(target, side_effect=replacement)
            mock.start()
            self.addCleanup(mock.stop)

    @property
    def headers(self) -> dict[str, str]:
        return {
            **_browser_mutation_headers(self.sessions, self.session),
            "Idempotency-Key": "integrated-retirement",
        }

    def fetch(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["target_id"] not in self.apps:
            raise DokployRequestFailed(
                method="GET", path="fixture", detail="Not found", status_code=404
            )
        return dict(self.apps[kwargs["target_id"]])

    def delete(self, **kwargs: Any) -> None:
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).lifecycle_state, "retiring"
        )
        self.deleted.append(kwargs["application_id"])
        self.apps.pop(kwargs["application_id"], None)

    def delete_resource(self, **kwargs: Any) -> None:
        self.delete(application_id=kwargs["resource_id"])

    async def plan(self) -> dict[str, Any]:
        response = await request(
            self.app, "POST", "/v1/product-retirement", headers=self.headers, payload=self.payload
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.deleted, [])
        result = response.json()
        self.assertEqual(
            {p["preview_id"] for p in result["result"]["preview_plan"]}, {"legacy-1", "legacy-6"}
        )
        self.apply_payload = {
            **self.payload,
            "mode": "apply",
            "reviewed_plan_record_id": result["records"]["product_retirement_plan_id"],
            "reviewed_plan_sha256": result["result"]["plan_sha256"],
            "confirmation": result["result"]["confirmation"],
        }
        return cast(dict[str, Any], result)

    async def test_retires_present_and_absent_previews_then_testing_and_excludes_sweep(
        self,
    ) -> None:
        await self.plan()
        response = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.deleted, ["preview-6", "testing-app"])
        self.assertTrue(response.json()["result"]["provider_absence_verified"])
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).lifecycle_state, "retired"
        )
        for preview in self.store.list_preview_records():
            self.assertEqual(preview.state, "destroyed")
            self.assertTrue(preview.destroyed_at)
        self.assertEqual(request_product_reconcile_sweep(self.store, self.profile.updated_at), ())
        replay = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(self.deleted, ["preview-6", "testing-app"])

    async def test_uncertain_preview_delete_resumes_same_request_without_second_delete(
        self,
    ) -> None:
        await self.plan()
        original = self.delete_resource

        def committed_then_lost(**kwargs: Any) -> None:
            original(**kwargs)
            raise TimeoutError("lost provider response")

        with patch(
            "control_plane.workflows.preview_resource_destroy._delete_preview_resource",
            side_effect=committed_then_lost,
        ):
            first = await request(
                self.app,
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=self.apply_payload,
            )
        self.assertEqual(first.status_code, 409, first.text)
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).lifecycle_state, "retiring"
        )
        retry = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(self.deleted, ["preview-6", "testing-app"])

    async def test_changed_preview_and_renamed_or_shared_provider_refuse_deletion(self) -> None:
        await self.plan()
        preview = self.store.read_preview_record("legacy-6")
        self.store.write_preview_record(
            preview.model_copy(update={"canonical_url": "https://changed.example.invalid"})
        )
        response = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.deleted, [])

    async def test_instance_grant_does_not_expand_to_product_preview_retirement(self) -> None:
        from tests.test_http_app_product_retirement import ProductRetirementHttpTests

        helper = ProductRetirementHttpTests()
        machine_app = helper._app(
            self.store, actions=("product_retirement.plan", "product_retirement.apply")
        )
        response = await request(
            machine_app,
            "POST",
            "/v1/product-retirement",
            headers=helper.headers,
            payload=self.payload,
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.deleted, [])

    async def test_provider_config_drift_and_unbound_renamed_candidates_fail_closed(self) -> None:
        await self.plan()
        self.apps["preview-6"]["dockerImage"] = self.profile.image.repository + ":changed"
        response = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.deleted, [])
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).lifecycle_state, "active"
        )

        new_headers = {**self.headers, "Idempotency-Key": "review-changed-image"}
        replanned = await request(
            self.app, "POST", "/v1/product-retirement", headers=new_headers, payload=self.payload
        )
        self.assertEqual(replanned.status_code, 202, replanned.text)
        updated = replanned.json()
        apply_request = {
            **self.payload,
            "mode": "apply",
            "reviewed_plan_record_id": updated["records"]["product_retirement_plan_id"],
            "reviewed_plan_sha256": updated["result"]["plan_sha256"],
            "confirmation": updated["result"]["confirmation"],
        }
        applied = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers={**self.headers, "Idempotency-Key": "apply-changed-image"},
            payload=apply_request,
        )
        self.assertEqual(applied.status_code, 202, applied.text)

    async def test_shared_preview_target_refuses_plan_before_any_delete(self) -> None:
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.profile.preview.context,
                instance="pr-6",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                target_id="preview-6",
                display_name="owned",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="other-product",
                instance="testing",
                target_id="preview-6",
                updated_at=self.profile.updated_at,
            )
        )
        response = await request(
            self.app, "POST", "/v1/product-retirement", headers=self.headers, payload=self.payload
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.deleted, [])
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).lifecycle_state, "active"
        )

    async def test_preview_absence_is_required_before_terminal_record_or_testing_delete(
        self,
    ) -> None:
        await self.plan()
        with patch(
            "control_plane.workflows.preview_resource_destroy._delete_preview_resource",
            return_value=None,
        ):
            response = await request(
                self.app,
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=self.apply_payload,
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.store.read_preview_record("legacy-6").state, "active")
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product).lifecycle_state, "retiring"
        )
        retry = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(retry.status_code, 202, retry.text)

    async def test_committed_preview_checkpoint_disconnect_resumes_without_duplicate_delete(
        self,
    ) -> None:
        from sqlalchemy.exc import OperationalError

        await self.plan()
        original = self.store.commit_product_retirement_preview

        def committed_then_lost(**kwargs: Any) -> None:
            original(**kwargs)
            raise OperationalError("fixture commit", {}, RuntimeError("connection lost"))

        with patch.object(
            self.store, "commit_product_retirement_preview", side_effect=committed_then_lost
        ):
            first = await request(
                self.app,
                "POST",
                "/v1/product-retirement",
                headers=self.headers,
                payload=self.apply_payload,
            )
        self.assertEqual(first.status_code, 409, first.text)
        self.assertEqual(self.store.read_preview_record("legacy-1").state, "destroyed")
        retry = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(self.deleted, ["preview-6", "testing-app"])

    async def test_unbound_renamed_product_candidate_blocks_plan(self) -> None:
        self.apps["orphan"] = {
            "applicationId": "orphan",
            "name": "renamed",
            "appName": "renamed-abc",
            "dockerImage": self.profile.image.repository + ":fixture",
            "applicationStatus": "idle",
        }
        response = await request(
            self.app, "POST", "/v1/product-retirement", headers=self.headers, payload=self.payload
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.deleted, [])

    async def test_tracked_preview_authority_is_removed_atomically_after_absence(self) -> None:
        context = self.profile.preview.context
        self.store.write_provider_target_record(
            ProviderTargetRecord(
                context=context,
                instance="pr-6",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                target_id="preview-6",
                display_name="owned",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=context,
                instance="pr-6",
                target_type="application",
                target_name="example-site-pr-6",
                updated_at=self.profile.updated_at,
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=context,
                instance="pr-6",
                target_id="preview-6",
                updated_at=self.profile.updated_at,
            )
        )
        await self.plan()
        response = await request(
            self.app,
            "POST",
            "/v1/product-retirement",
            headers=self.headers,
            payload=self.apply_payload,
        )
        self.assertEqual(
            response.status_code,
            202,
            response.text
            + str(
                [
                    (r.outcome, r.mutation_evidence.error_message)
                    for r in self.store.list_product_retirement_records(
                        product=self.profile.product
                    )
                ]
            ),
        )
        self.assertEqual(self.store.list_provider_target_records(), ())
        self.assertEqual(self.store.list_dokploy_target_id_records(), ())
        self.assertEqual(self.store.list_dokploy_target_records(), ())

    async def test_standalone_destroy_with_stale_profile_refuses_retiring_product(self) -> None:
        from control_plane.workflows.generic_web_preview import (
            execute_generic_web_preview_destroy,
            GenericWebPreviewDestroyRequest,
        )

        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lifecycle_state": "retiring",
                    "preview": self.profile.preview.model_copy(update={"enabled": False}),
                }
            )
        )
        with patch(
            "control_plane.workflows.generic_web_preview._execute_generic_web_preview_destroy_unserialized"
        ) as provider:
            result = execute_generic_web_preview_destroy(
                control_plane_root=self.root,
                record_store=self.store,
                request=GenericWebPreviewDestroyRequest(
                    product=self.profile.product,
                    preview_slug="pr-6",
                    destroy_reason="Fixture lifecycle fence",
                ),
                profile=self.profile,
            )
            self.assertEqual(result.destroy_status, "fail")
            provider.assert_not_called()

    async def test_ordinary_operations_coexist_and_retirement_drains_them(self) -> None:
        if self.store.database_url.startswith("sqlite"):
            self.skipTest("PostgreSQL shared/exclusive advisory-lock proof.")
        import asyncio
        from threading import Event
        from control_plane.workflows.generic_web_preview import (
            serialize_generic_web_product_operation,
        )

        entered = (Event(), Event())
        release = Event()
        retired = Event()

        def ordinary(index: int) -> None:
            with serialize_generic_web_product_operation(
                record_store=self.store, product=self.profile.product
            ):
                entered[index].set()
                if not release.wait(5):
                    raise AssertionError("Timed out releasing ordinary operation.")

        def retirement() -> None:
            with serialize_generic_web_product_operation(
                record_store=self.store, product=self.profile.product, exclusive=True
            ):
                retired.set()

        tasks = [asyncio.create_task(asyncio.to_thread(ordinary, i)) for i in range(2)]
        try:
            for event in entered:
                self.assertTrue(await asyncio.to_thread(event.wait, 3))
            tasks.append(asyncio.create_task(asyncio.to_thread(retirement)))
            self.assertFalse(await asyncio.to_thread(retired.wait, 0.1))
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
        self.assertTrue(retired.is_set())

    async def test_retiring_product_fences_a_queued_deploy_with_stale_profile(self) -> None:
        if self.store.database_url.startswith("sqlite"):
            self.skipTest("PostgreSQL advisory-lock integration proof; SQLite is portability only.")
        import asyncio
        from threading import Event
        from control_plane.workflows.generic_web_preview import (
            serialize_generic_web_product_operation,
        )
        from control_plane.workflows.generic_web_deploy import (
            execute_generic_web_deploy,
            GenericWebDeployRequest,
        )
        import click

        entered = Event()

        def queued_deploy() -> None:
            entered.set()
            with self.assertRaises(click.ClickException):
                execute_generic_web_deploy(
                    control_plane_root=self.root,
                    record_store=self.store,
                    request=GenericWebDeployRequest(
                        product=self.profile.product,
                        instance="testing",
                        artifact_id="fixture",
                        source_git_ref="main",
                    ),
                    profile=self.profile,
                )

        with serialize_generic_web_product_operation(
            record_store=self.store, product=self.profile.product, exclusive=True
        ):
            task = asyncio.create_task(asyncio.to_thread(queued_deploy))
            await asyncio.to_thread(entered.wait, 5)
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={
                        "lifecycle_state": "retiring",
                        "preview": self.profile.preview.model_copy(update={"enabled": False}),
                    }
                )
            )
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.deleted, [])
