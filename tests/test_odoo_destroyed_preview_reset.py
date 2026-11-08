import unittest
from unittest.mock import patch

from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimeTargetEvidence
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetPolicies,
    DokployTargetStaffTestingHold,
)
from control_plane.disposable_lanes import (
    DisposableLaneSafetyUnknown,
    require_disposable_preview_operation,
    resolve_disposable_preview,
)
from control_plane.odoo_preview_apply_http import _build_odoo_preview_apply_inputs
from control_plane.odoo_preview_resources import preview_resource_environment
from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsRequest
from tests.test_director_bounded_writes import DirectorBoundedWritesFixture
from tests.support.stores import _write_odoo_preview_template_runtime_environment


class DestroyedPreviewResetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = DirectorBoundedWritesFixture(self, record_source_history=False)
        self.fixture.prepare_destroyed_preview_creation()
        self.fixture.preview = self.fixture.preview.model_copy(
            update={"latest_generation_id": "", "active_generation_id": ""}
        )
        self.fixture.store.write_preview_record(self.fixture.preview)
        _write_odoo_preview_template_runtime_environment(store=self.fixture.store)
        previous = self.fixture.payload["apply"]["dry_run_plan"]
        target = OdooPreviewRuntimeTargetEvidence(
            target_id="remaining-preview",
            target_name=previous["compose_name"],
            context=self.fixture.preview.context,
            instance=previous["preview_slug"],
            environment_kind="preview",
            domain=previous["domain_host"],
        )
        self.enterContext(
            patch(
                "control_plane.workflows.odoo_preview_runtime.discover_odoo_preview_target",
                return_value=target,
            )
        )
        self.enterContext(
            patch(
                "control_plane.workflows.odoo_preview_runtime._preview_template_compose_id",
                return_value="preview-template",
            )
        )
        self.plan = _build_odoo_preview_apply_inputs(
            control_plane_root=self.fixture.root,
            record_store=self.fixture.store,
            profile=self.fixture.profile,
            request=OdooPreviewApplyInputsRequest(
                product=self.fixture.profile.product,
                operation="destroy",
                pr_number=self.fixture.preview.anchor_pr_number,
                preview_url=previous["preview_url"],
            ),
            database_url=None,
        )
        self.assertEqual(self.plan.status, "ready", self.plan.error_message)
        self.assertEqual(self.plan.dry_run_plan.compose_ref, target.target_id)
        self.assertTrue(self.plan.dry_run_plan.delete_volumes)
        self.fixture.payload["apply"]["dry_run_plan"] = self.plan.dry_run_plan.model_dump(
            mode="json"
        )
        self.fixture.payload["apply"].pop("manifest", None)
        self.fixture.payload["apply"].pop("image_reference", None)
        self.fixture.issue_preview(self.plan)

    def test_remaining_target_reaches_admission_without_history_reconstruction(self) -> None:
        before = self.fixture.store.read_preview_record(self.fixture.preview.preview_id)
        require_disposable_preview_operation(
            self.fixture.store, profile=self.fixture.profile, issued_plan=self.plan
        )
        self.assertEqual(self.fixture.store.read_preview_record(before.preview_id), before)
        self.assertIsNone(
            self.fixture.store.read_disposable_lane_safety(
                self.fixture.profile.product, before.context, self.plan.preview_slug
            )
        )
        with self.assertRaises(DisposableLaneSafetyUnknown):
            resolve_disposable_preview(
                self.fixture.store, self.fixture.profile, before.context, self.plan.preview_slug
            )

    def test_destroyed_target_retains_protected_use_data_and_resources(self) -> None:
        for protection in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
            {"production_resources": "separation_ready"},
        ):
            with self.subTest(protection=protection):
                self.fixture.safety(**protection)
                with self.assertRaises(DisposableLaneSafetyUnknown):
                    require_disposable_preview_operation(
                        self.fixture.store, profile=self.fixture.profile, issued_plan=self.plan
                    )

    def test_destroyed_target_retains_staff_hold(self) -> None:
        self.fixture.store.write_dokploy_target_record(
            DokployTargetRecord(
                context=self.fixture.preview.context,
                instance=self.plan.preview_slug,
                updated_at=self.fixture.profile.updated_at,
                policies=DokployTargetPolicies(
                    staff_testing_hold=DokployTargetStaffTestingHold(reason="Staff are testing")
                ),
            )
        )
        with self.assertRaises(DisposableLaneSafetyUnknown):
            require_disposable_preview_operation(
                self.fixture.store, profile=self.fixture.profile, issued_plan=self.plan
            )

    def test_destroyed_target_refuses_foreign_or_ambiguous_lifecycle(self) -> None:
        preview = self.fixture.preview
        for candidates in (
            (),
            (preview, preview.model_copy(update={"preview_id": "another-preview"})),
            (preview.model_copy(update={"anchor_repo": "foreign/repository"}),),
        ):
            with (
                self.subTest(candidates=candidates),
                patch.object(self.fixture.store, "list_preview_records", return_value=candidates),
                self.assertRaises(DisposableLaneSafetyUnknown),
            ):
                require_disposable_preview_operation(
                    self.fixture.store, profile=self.fixture.profile, issued_plan=self.plan
                )

    def test_destroyed_target_refuses_physical_production_alias(self) -> None:
        self.fixture.store.write_provider_target_record(
            ProviderTargetRecord(
                context=self.fixture.preview.context,
                instance="prod",
                display_name="production",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="compose",
                target_id=self.plan.dry_run_plan.compose_ref,
                updated_at=self.fixture.profile.updated_at,
            )
        )
        with self.assertRaises(DisposableLaneSafetyUnknown):
            require_disposable_preview_operation(
                self.fixture.store, profile=self.fixture.profile, issued_plan=self.plan
            )

    async def test_destroyed_target_still_needs_current_native_resource_proof(self) -> None:
        from control_plane.dokploy import api, post_deploy, source

        plan = self.plan.dry_run_plan
        environment = preview_resource_environment(plan.compose_name)
        provider = {
            "composeId": plan.compose_ref,
            "appName": "remaining-project",
            "serverId": "preview-server",
            "sourceType": "raw",
            "composePath": "docker-compose.yml",
            "composeFile": (
                "services:\n  web:\n    image: fixture\nvolumes:\n"
                "  data:\n    name: ${ODOO_DATA_VOLUME:?missing}\n"
                "  db:\n    name: ${ODOO_DB_VOLUME:?missing}\n"
                "  logs:\n    name: ${ODOO_LOG_VOLUME:?missing}\n"
            ),
            "env": "\n".join(f"{key}={value}" for key, value in environment.items()),
        }
        with (
            patch.object(source, "read_dokploy_config", return_value=("fixture", "fixture")),
            patch.object(api, "fetch_dokploy_target_payload", return_value=provider),
            patch.object(api, "find_matching_dokploy_schedule", return_value=None),
            patch.object(post_deploy, "run_manual_data_schedule", return_value={}) as inspect,
            patch(
                "control_plane.workflows.preview_resource_destroy.destroy_dokploy_resource"
            ) as destroy,
        ):
            response = await self.fixture.preview_apply()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["status"], "blocked")
        inspect.assert_called_once()
        destroy.assert_not_called()
        self.assertEqual(
            self.fixture.store.read_preview_record(self.fixture.preview.preview_id),
            self.fixture.preview,
        )

    async def test_completed_destroy_advances_lifecycle_and_replays_without_another_effect(
        self,
    ) -> None:
        plan = self.plan.dry_run_plan
        with patch(
            "control_plane.http_app.execute_odoo_preview_apply_result",
            return_value={
                "status": "pass",
                "operation": plan.operation,
                "product": plan.product,
                "repository": plan.repository,
                "preview_slug": plan.preview_slug,
                "preview_url": plan.preview_url,
                "domain_host": plan.domain_host,
                "compose_name": plan.compose_name,
                "compose_id": plan.compose_ref,
            },
        ) as execute:
            response = await self.fixture.preview_apply()
            self.assertEqual(response.status_code, 202, response.text)
            completed = self.fixture.store.read_preview_record(self.fixture.preview.preview_id)
            self.assertEqual(completed.state, "destroyed")
            self.assertNotEqual(completed.destroy_reason, self.fixture.preview.destroy_reason)
            replay = await self.fixture.preview_apply()
        self.assertEqual(replay.status_code, 202, replay.text)
        execute.assert_called_once()
        self.assertEqual(self.fixture.store.read_preview_record(completed.preview_id), completed)
