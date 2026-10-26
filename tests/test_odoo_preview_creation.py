from contextlib import ExitStack, redirect_stdout
import io
import json
from datetime import datetime, timedelta, timezone
import subprocess
import sys
from typing import Any
from control_plane.provider_operations import ProviderMutationUnknownError
import unittest
from unittest.mock import patch

from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimeBindingEvidence
from control_plane.odoo_preview_apply_http import (
    OdooPreviewApplyEnvelope,
    _OdooPreviewServiceEnvironment,
    _build_odoo_preview_apply_inputs,
)
from control_plane.odoo_preview_resources import (
    PREVIEW_RESOURCE_PROGRAM,
    preview_resource_environment,
)
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_REQUIRED_ENV_KEYS,
    OdooPreviewApplyInputsRequest,
    ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS,
)
from tests.test_director_bounded_writes import DirectorBoundedWritesFixture
from tests.test_odoo_preview_runtime import _environment_values, _module_update_evidence


class InterruptedPreviewCreationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = DirectorBoundedWritesFixture(self)
        self.fixture.prepare_destroyed_preview_creation()
        self.compose: dict[str, Any] | None = None
        self.domains: list[dict[str, Any]] = []
        self.mutations: list[str] = []
        self.interrupt = True
        self.stop_after = "domain"
        self.active_schedule = False
        self.storage_appears = False
        self.host_volumes: set[str] = set()
        self.host_volume_projects: dict[str, str | None] = {}
        self.template_id = "preview-template"
        self.server_id = "preview-server"
        self.environment_id = "preview-environment"
        self.url = self.fixture.payload["apply"]["dry_run_plan"]["preview_url"]
        self.template = {
            "composeId": self.template_id,
            "appName": "template-project",
            "serverId": self.server_id,
            "composeType": "docker-compose",
            "deployments": [],
        }
        self.environment = _environment_values()
        name = self.fixture.payload["apply"]["dry_run_plan"]["compose_name"]
        self.environment.update(preview_resource_environment(name))
        stack = self.enterContext(ExitStack())
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://provider.example.test", "fixture"),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.resolve_odoo_preview_url",
                return_value=self.url,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime._preview_template_compose_id",
                return_value=self.template_id,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime._preview_environment_id",
                return_value=(self.environment_id, ""),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime._preview_runtime_bindings",
                return_value=tuple(
                    OdooPreviewRuntimeBindingEvidence(key=key, source="managed_secret")
                    for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS
                ),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.odoo_preview_apply_http._odoo_preview_service_environment",
                side_effect=lambda **_kwargs: _OdooPreviewServiceEnvironment(
                    values=dict(self.environment)
                ),
            )
        )
        stack.enter_context(patch("control_plane.lane_movement.require_forward_preview_build"))
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_api.dokploy_request",
                side_effect=self.provider,
            )
        )
        stack.enter_context(
            patch("control_plane.workflows.odoo_preview_runtime.require_bridge_network_capacity")
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.memory_capacity.require_preview_host_memory_capacity"
            )
        )
        stack.enter_context(
            patch(
                "control_plane.dokploy.post_deploy._resolve_dokploy_schedule_runtime",
                side_effect=lambda **kwargs: (
                    "dokploy-server",
                    self.server_id,
                    kwargs["target_payload"]["appName"],
                    self.server_id,
                ),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.dokploy.api.find_matching_dokploy_schedule",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.dokploy.post_deploy.run_manual_data_schedule",
                side_effect=self.inspect_host,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.dokploy_post_deploy.run_compose_post_deploy_update",
                return_value=_module_update_evidence(),
            )
        )
        stack.enter_context(
            patch("control_plane.workflows.odoo_preview_runtime._wait_for_smoke_check")
        )
        envelope = OdooPreviewApplyEnvelope.model_validate(self.fixture.payload)
        self.inputs = OdooPreviewApplyInputsRequest(
            product=self.fixture.profile.product,
            pr_number=self.fixture.preview.anchor_pr_number,
            preview_url=self.url,
            manifest=envelope.apply.manifest,
            source_git_ref=envelope.apply.manifest.source_commit if envelope.apply.manifest else "",
            source="fixture-native-creation",
        )
        planned = self.plan()
        self.assertEqual(planned.status, "ready")
        self.fixture.payload["apply"]["dry_run_plan"] = planned.dry_run_plan.model_dump(mode="json")
        self.fixture.issue_preview(planned)

    def plan(self) -> Any:
        return _build_odoo_preview_apply_inputs(
            control_plane_root=self.fixture.root,
            record_store=self.fixture.store,
            profile=self.fixture.profile,
            request=self.inputs,
            database_url=None,
        )

    def provider(self, **kwargs: Any) -> Any:
        path = kwargs["path"]
        payload = kwargs.get("payload", {})
        if kwargs.get("method") == "POST":
            self.mutations.append(path)
        if path == "/api/project.all":
            return [
                {
                    "environments": [
                        {
                            "environmentId": self.environment_id,
                            "composes": [self.compose] if self.compose else [],
                        }
                    ]
                }
            ]
        if path == "/api/compose.search":
            return [self.compose] if self.compose else []
        if path == "/api/domain.byComposeId":
            return self.domains
        if path == "/api/compose.one":
            if kwargs["query"]["composeId"] == self.template_id:
                return dict(self.template)
            self.assertIsNotNone(self.compose)
            return dict(self.compose or {})
        if path == "/api/compose.create":
            self.assertIsNone(self.compose)
            self.compose = {
                **payload,
                "composeId": "created-recipient",
                "appName": payload["appName"] + "-fixture",
                "composeFile": "",
                "env": "",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "deployments": [],
            }
            if self.interrupt and self.stop_after == "create":
                self.interrupt = False
                raise ProviderMutationUnknownError("Fixture interrupted after compose creation")
            return dict(self.compose)
        if path == "/api/compose.update":
            assert self.compose is not None
            self.compose.update(payload)
            return dict(self.compose)
        if path in {"/api/domain.create", "/api/domain.update"}:
            domain = {**payload, "domainId": "preview-domain"}
            self.domains = [domain]
            if self.storage_appears:
                name = self.environment["ODOO_DATA_VOLUME"]
                self.host_volumes.add(name)
                directory = self.fixture.root.resolve() / "volumes" / name / "_data"
                directory.mkdir(parents=True)
                (directory / "needed-data").write_text("retained-only-copy")
            if self.interrupt and self.stop_after == "domain":
                self.interrupt = False
                raise ProviderMutationUnknownError(
                    "Fixture process interrupted after domain creation"
                )
            return domain
        if path == "/api/schedule.list":
            if not self.active_schedule:
                return []
            from control_plane.dokploy.post_deploy import (
                _build_dokploy_data_workflow_schedule_app_name,
            )

            return [
                {
                    "name": "fixture-data-workflow",
                    "appName": _build_dokploy_data_workflow_schedule_app_name(
                        context_name=self.fixture.profile.product,
                        instance_name=self.fixture.payload["apply"]["dry_run_plan"]["preview_slug"],
                    ),
                    "deployments": [{"deploymentId": "active-data-job", "status": "running"}],
                }
            ]
        if path == "/api/compose.deploy":
            assert self.compose is not None
            self.compose["deployments"].append(
                {
                    "deploymentId": "preview-deployment",
                    "status": "done",
                    "title": payload.get("title", ""),
                }
            )
            return {}
        self.fail(f"Unexpected provider operation: {path}")

    def docker(self, command: list[str], **_kwargs: Any) -> Any:
        args = command[1:]
        if args[:2] == ["volume", "ls"]:
            value = "\n".join(sorted(self.host_volumes))
        elif args[:2] == ["volume", "inspect"]:
            name = args[-1]
            self.assertIn(name, self.host_volumes)
            value = json.dumps(
                {
                    "Name": name,
                    "Driver": "local",
                    "HasOptions": False,
                    "Project": self.host_volume_projects.get(
                        name, (self.compose or {}).get("appName")
                    ),
                    "Mountpoint": str(self.fixture.root.resolve() / "volumes" / name / "_data"),
                }
            )
        elif args[0] == "ps":
            value = ""
        elif args[0] == "info":
            value = str(self.fixture.root.resolve())
        else:
            self.fail(f"Unexpected host operation: {args}")
        return subprocess.CompletedProcess(command, 0, value, "")

    def inspect_host(self, **kwargs: Any) -> dict[str, str]:
        script = kwargs["schedule_payload"]["script"]
        encoded = script.split("python3 - ")[1].split(" ")[0]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["fixture-inspection", encoded]),
            patch("subprocess.run", side_effect=self.docker),
            patch("signal.signal"),
            patch("signal.alarm"),
            redirect_stdout(output),
        ):
            try:
                exec(compile(PREVIEW_RESOURCE_PROGRAM, "<fixture-inspection>", "exec"), {})
            except SystemExit:
                pass
        return dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def original_operation(self) -> Any:
        from control_plane.http_app import idempotency_scope
        from control_plane.service_auth import configured_local_operator_identity
        from control_plane.odoo_preview_apply_http import ODOO_PREVIEW_APPLY_ROUTE

        identity = configured_local_operator_identity(self.fixture.config)
        assert identity is not None
        record = self.fixture.store.read_idempotency_record(
            scope=idempotency_scope(identity),
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=self.fixture.plan_id,
        )
        assert record is not None
        return record

    async def test_original_create_resumes_after_domain_without_another_compose(self) -> None:
        first = await self.fixture.preview_apply()
        self.assertEqual(first.status_code, 409, first.text)
        self.assertEqual(first.json()["error"]["code"], "mutation_reconciliation_required")
        original = self.original_operation()
        intent = self.fixture.store.read_original_mutation_intent(original)
        first_history = self.fixture.store.read_preview_record(self.fixture.preview.preview_id)
        self.assertEqual(first_history.state, "destroyed")
        before = OdooPreviewApplyEnvelope.model_validate(self.fixture.payload).apply.dry_run_plan
        self.assertEqual(self.plan().dry_run_plan, before)
        result = await self.fixture.preview_apply()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()["result"]["status"], "pass")
        self.assertEqual(self.mutations.count("/api/compose.create"), 1)
        self.assertEqual(self.mutations.count("/api/compose.deploy"), 1)
        completed = self.original_operation()
        self.assertEqual(completed.record_id, original.record_id)
        self.assertGreater(completed.attempt, original.attempt)
        self.assertEqual(self.fixture.store.read_original_mutation_intent(completed), intent)
        preview = self.fixture.store.read_preview_record(self.fixture.preview.preview_id)
        self.assertEqual(preview.state, "active")
        self.assertTrue(preview.serving_generation_id)

    async def test_original_issued_rollback_metadata_survives_deploy_and_continuation(self) -> None:
        import json
        from pathlib import Path
        from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsResult

        legacy = json.loads(
            (
                Path(__file__).parent / "fixtures" / "odoo_preview_issued_creation_rollback.json"
            ).read_text()
        )
        value = self.plan().model_dump(mode="json")
        for field, metadata in legacy.items():
            value[field].update(metadata)
        original = OdooPreviewApplyInputsResult.model_validate(value)
        self.fixture.payload["apply"]["dry_run_plan"] = original.dry_run_plan.model_dump(
            mode="json"
        )
        self.fixture.issue_preview(original)
        first = await self.fixture.preview_apply()
        self.assertEqual(first.status_code, 409, first.text)
        self.assertEqual(first.json()["error"]["code"], "mutation_reconciliation_required")
        result = await self.fixture.preview_apply()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()["result"]["status"], "pass")
        self.assertFalse(any(path.endswith(".delete") for path in self.mutations))

    async def test_protection_blocks_original_continuation_without_another_effect(self) -> None:
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        mutations = list(self.mutations)
        self.fixture.safety(usage="in_use")
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        self.assertEqual(self.mutations, mutations)

    async def test_partial_create_uses_only_guarded_expired_original_supersession(self) -> None:
        self.stop_after = "create"
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        original = self.original_operation()
        intent = self.fixture.store.read_original_mutation_intent(original)
        self.fixture.plan_id = "odoo-preview-plan-qualified-partial"
        self.fixture.issue_preview(self.plan())
        mutations = list(self.mutations)

        def age(seconds: int) -> None:
            from sqlalchemy import select
            from control_plane.storage.postgres import LaunchplaneIdempotencyRow

            current = self.fixture.store.read_idempotency_record_by_id(original.record_id)
            assert current is not None
            replacement = current.model_copy(
                update={
                    "lease_expires_at": (
                        datetime.now(timezone.utc) - timedelta(seconds=seconds)
                    ).isoformat(),
                }
            )
            with self.fixture.store._session_factory() as session:
                row = session.scalar(
                    select(LaunchplaneIdempotencyRow).where(
                        LaunchplaneIdempotencyRow.record_id == original.record_id
                    )
                )
                assert row is not None
                self.fixture.store._sync_idempotency_row(row, replacement)
                session.commit()

        for seconds in (-60, 0):
            age(seconds)
            self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
            self.assertEqual(self.mutations, mutations)
        age(ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS + 60)
        self.active_schedule = True
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        self.assertEqual(self.mutations, mutations)
        self.active_schedule = False
        result = await self.fixture.preview_apply()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()["result"]["status"], "pass")
        settled = self.fixture.store.read_idempotency_record_by_id(original.record_id)
        assert settled is not None
        self.assertEqual(settled.state, "completed")
        self.assertEqual(self.fixture.store.read_original_mutation_intent(settled), intent)
        self.assertEqual(self.mutations.count("/api/compose.create"), 1)
        self.assertEqual(self.mutations.count("/api/compose.deploy"), 1)

    async def test_unknown_history_deployment_or_server_keeps_existing_target(self) -> None:
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        assert self.compose is not None
        mutations = list(self.mutations)
        for history in (
            None,
            [{"deploymentId": "queued", "status": "queued"}],
            [{"deploymentId": "finished", "status": "done"}],
        ):
            with self.subTest(history=history):
                self.compose["deployments"] = history
                self.assertIsNotNone(self.plan().runtime_plan.target)
                self.assertEqual(self.mutations, mutations)
        self.compose["deployments"] = []
        self.compose["serverId"] = "another-server"
        self.assertIsNotNone(self.plan().runtime_plan.target)
        self.assertEqual(self.mutations, mutations)

    def enable_clean_snapshot(self) -> None:
        from control_plane.clean_testing_snapshot import CleanTestingSnapshot
        from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget

        profile = self.fixture.profile.model_copy(deep=True)
        profile.preview.clean_testing_snapshot = True
        budget = OdooTestingDrillStorageBudget(
            database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
        )
        profile.lanes = tuple(
            lane.model_copy(update={"odoo_testing_drill_storage": budget})
            if lane.instance == "testing"
            else lane
            for lane in profile.lanes
        )
        self.fixture.profile = profile
        self.fixture.store.write_product_profile_record(profile)
        snapshot = CleanTestingSnapshot(
            product=profile.product,
            context=profile.preview.context,
            server_id=self.server_id,
            snapshot_id="a" * 64,
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            captured_at=datetime.now(timezone.utc).isoformat(),
            storage_budget=budget,
        )
        self.fixture.store.write_clean_testing_snapshot(snapshot)

    async def test_unavailable_snapshot_custody_cannot_normalize_an_existing_target(self) -> None:
        import click

        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        mutations = list(self.mutations)
        self.enable_clean_snapshot()
        with (
            patch(
                "control_plane.preview_filestore.prepare_preview_filestore",
                side_effect=click.ClickException("Needed foreign snapshot storage"),
            ) as prepare,
        ):
            self.assertIsNotNone(self.plan().runtime_plan.target)
        prepare.assert_called_once()
        self.assertEqual(self.mutations, mutations)

    async def test_missing_snapshot_declaration_cannot_admit_retained_storage(self) -> None:
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        self.enable_clean_snapshot()
        name = self.environment["ODOO_DB_VOLUME"]
        self.host_volumes.add(name)
        directory = self.fixture.root.resolve() / "volumes" / name / "_data"
        directory.mkdir(parents=True)
        needed = directory / "needed-data"
        needed.write_text("preserve me")
        with patch("control_plane.preview_filestore.prepare_preview_filestore", return_value=None):
            self.assertIsNotNone(self.plan().runtime_plan.target)
        self.assertEqual(needed.read_text(), "preserve me")

    async def test_snapshot_partial_allocation_requires_each_project_owner(self) -> None:
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        self.enable_clean_snapshot()
        assert self.compose is not None
        for key in ("ODOO_LOG_VOLUME", "ODOO_DB_VOLUME"):
            name = self.environment[key]
            directory = self.fixture.root.resolve() / "volumes" / name / "_data"
            directory.mkdir(parents=True)
            self.host_volumes.add(name)
            try:
                for project in ("foreign-project", None, self.compose["appName"]):
                    with self.subTest(resource=key, project=project):
                        self.host_volume_projects[name] = project
                        with patch(
                            "control_plane.preview_filestore.prepare_preview_filestore",
                            return_value={
                                "name": self.environment["ODOO_DATA_VOLUME"] + "-filestore"
                            },
                        ):
                            plan = self.plan()
                        self.assertEqual(
                            plan.runtime_plan.target is None, project == self.compose["appName"]
                        )
            finally:
                self.host_volumes.remove(name)
                directory.rmdir()
                directory.parent.rmdir()

    async def test_deployed_recipient_is_refused_before_snapshot_host_work(self) -> None:
        self.assertEqual((await self.fixture.preview_apply()).status_code, 409)
        self.enable_clean_snapshot()
        assert self.compose is not None
        self.compose["deployments"] = [{"deploymentId": "busy", "status": "queued"}]
        with patch(
            "control_plane.preview_filestore.prepare_preview_filestore",
            side_effect=AssertionError("Deployed recipients cannot acquire snapshot locks"),
        ):
            self.assertIsNotNone(self.plan().runtime_plan.target)

    async def test_refused_new_storage_is_neither_deleted_nor_deployed(self) -> None:
        self.interrupt = False
        self.storage_appears = True
        result = await self.fixture.preview_apply()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "mutation_reconciliation_required")
        original = self.original_operation()
        self.assertEqual(original.state, "reconcile_required")
        self.assertEqual(original.provider_effect_phase, "domain_ensure")
        self.assertNotIn("/api/compose.delete", self.mutations)
        self.assertNotIn("/api/domain.delete", self.mutations)
        self.assertNotIn("/api/compose.deploy", self.mutations)
        self.assertIsNotNone(self.compose)
        self.assertTrue(self.domains)
        path = (
            self.fixture.root.resolve()
            / "volumes"
            / self.environment["ODOO_DATA_VOLUME"]
            / "_data"
            / "needed-data"
        )
        self.assertEqual(path.read_text(), "retained-only-copy")
