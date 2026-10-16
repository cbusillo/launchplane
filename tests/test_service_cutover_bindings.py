"""Installed host admission and native consumer assembly at service boundaries."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import MagicMock, patch
from click.testing import CliRunner

from control_plane.cli import main
from control_plane import service_bootstrap
from control_plane.http_app import ResolvedLaunchplaneAuthzPolicy
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerLoopResult,
    OdooStableOperationWorkerResult,
)

from control_plane.dokploy.service_cutover import (
    OdooHostCapabilities,
    bind_odoo_driver,
    installed_cutover_host,
    service_odoo_cutover_factory,
    service_web_cutover_factory,
)
from control_plane.dokploy.slot_writers import ContainerSlotWriters, SlotWriterContainer
from control_plane.workflows.generic_web_deploy_provider import default_generic_web_deploy_provider
from control_plane.generic_web_cutover import compile_web_cutover
from tests import test_generic_web_cutover as web_fixture
from tests import test_generic_web_cutover_provider as web_provider_fixture
from tests import test_odoo_cutover_website_provider as odoo_fixture


class ServiceCutoverBindingTests(unittest.TestCase):
    def test_service_bootstrap_and_both_worker_commands_supply_lazy_factory(self) -> None:
        fixture = self.fixture()
        root = Path(fixture.temporary.name)
        store: Any = MagicMock()
        policy = LaunchplaneAuthzPolicy()
        with (
            patch.object(
                service_bootstrap,
                "resolve_launchplane_authz_policy",
                return_value=ResolvedLaunchplaneAuthzPolicy(
                    policy=policy, policy_sha256="fixture", source="fixture"
                ),
            ),
            patch.object(service_bootstrap, "create_launchplane_fastapi_app") as app,
            patch.object(service_bootstrap, "load_github_oauth_config_from_env", return_value=None),
            patch("control_plane.dokploy.service_cutover.installed_cutover_host") as host,
            patch(
                "control_plane.dokploy.cutover_factory.resolve_profile_cutover_plan",
                return_value=None,
            ),
        ):
            service_bootstrap.create_launchplane_service_application(
                state_dir=root,
                bootstrap_authz_policy=policy,
                verifier=MagicMock(),
                service_record_store=store,
                control_plane_root_path=root,
            )
            factory = app.call_args.kwargs["odoo_cutover_provider_factory"]
            host.assert_not_called()
            with self.assertRaisesRegex(ValueError, "persisted lane/artifact authority"):
                factory(fixture.plan)
        for command, function, result in (
            (
                "run-once",
                "run_odoo_stable_operation_worker_once",
                OdooStableOperationWorkerResult(status="idle", operation_kind="", operation_id=""),
            ),
            (
                "run",
                "run_odoo_stable_operation_worker_loop",
                OdooStableOperationWorkerLoopResult(
                    status="completed", iterations=1, worked_count=0, idle_count=1, error_count=0
                ),
            ),
        ):
            with (
                self.subTest(command=command),
                patch("control_plane.cli_service._store", return_value=store),
                patch("control_plane.cli_service." + function, return_value=result) as worker,
                patch("control_plane.dokploy.service_cutover.installed_cutover_host") as load,
                patch(
                    "control_plane.dokploy.cutover_factory.resolve_profile_cutover_plan",
                    return_value=None,
                ) as authority,
            ):
                outcome = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        command,
                        "--database-url",
                        "postgresql://unused",
                        "--control-plane-root",
                        str(root),
                    ],
                )
                self.assertEqual(outcome.exit_code, 0, outcome.output)
                self.assertIs(worker.call_args.kwargs["record_store"], store)
                load.assert_not_called()
                with self.assertRaisesRegex(ValueError, "persisted lane/artifact authority"):
                    worker.call_args.kwargs["cutover_provider_factory"](fixture.plan)
                self.assertIs(authority.call_args.args[0], store)

    def fixture(self) -> odoo_fixture.WebsiteProviderAdapterTests:
        fixture = odoo_fixture.WebsiteProviderAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_missing_or_ambiguous_installed_host_never_loads_code(self) -> None:
        entry: Any = MagicMock()
        for candidates in ((), (entry, entry)):
            with (
                self.subTest(count=len(candidates)),
                patch(
                    "control_plane.dokploy.service_cutover.entry_points", return_value=candidates
                ),
                self.assertRaisesRegex(ValueError, "cutover_host_capability_unavailable"),
            ):
                installed_cutover_host()
        entry.load.assert_not_called()
        with patch("control_plane.dokploy.service_cutover.entry_points", return_value=(entry,)):
            self.assertIs(installed_cutover_host(), entry.load.return_value.return_value)

    def test_native_maintenance_settings_and_handoff_share_live_writer_inventory(self) -> None:
        fixture = self.fixture()
        plan = fixture.plan
        execute: Any = MagicMock()
        targets = tuple(
            SlotWriterContainer(writer, slot.slot_id, slot.slot_id + writer.writer_id, slot.image)
            for slot in (plan.old, plan.new)
            for writer in plan.writers
        )
        writers = ContainerSlotWriters(plan, targets, execute, lambda _target: 0)
        runner: Any = MagicMock(return_value="first-runner")
        startup: Any = MagicMock()
        preflight: Any = MagicMock()
        capabilities = OdooHostCapabilities(
            fixture.driver, writers, execute, runner, startup, preflight
        )
        bound = bind_odoo_driver(plan, capabilities)
        assert bound.website_settings_preflight is not None
        assert bound.website_settings is not None
        bound.website_settings_preflight(plan)
        preflight.assert_called_once_with(plan)
        with (
            patch(
                "control_plane.dokploy.service_cutover.run_planned_slot_maintenance"
            ) as maintenance,
            patch("control_plane.dokploy.service_cutover.run_slot_website_settings") as settings,
            patch.object(writers, "handoff", return_value=()) as handoff,
        ):
            bound.maintenance(plan)
            runner.return_value = "recreated-runner"
            assert plan.rollback_website_settings is not None
            bound.website_settings(plan, plan.old, plan.rollback_website_settings)
            bound.handoff_writers(plan, plan.old)
        self.assertIs(maintenance.call_args.kwargs["writers"], writers)
        self.assertIs(settings.call_args.kwargs["writers"], writers)
        self.assertIs(settings.call_args.kwargs["configure_startup"], startup)
        self.assertEqual(settings.call_args.kwargs["runner_container"], "recreated-runner")
        handoff.assert_called_once_with(plan.old)
        missing = bind_odoo_driver(plan, replace(capabilities, startup=None))
        assert missing.website_settings_preflight is not None
        with self.assertRaisesRegex(ValueError, "startup capability is unavailable"):
            missing.website_settings_preflight(plan)

    def test_odoo_rejects_stale_lane_before_loading_host(self) -> None:
        fixture = self.fixture()
        store: Any = MagicMock()
        with (
            patch(
                "control_plane.dokploy.cutover_factory.resolve_profile_cutover_plan",
                return_value=None,
            ),
            patch("control_plane.dokploy.service_cutover.installed_cutover_host") as host,
            self.assertRaisesRegex(ValueError, "persisted lane/artifact authority"),
        ):
            service_odoo_cutover_factory(store, Path(fixture.temporary.name))(fixture.plan)
        host.assert_not_called()

        # A second profile read can lose the lane after the plan's admission.
        store.read_product_profile_record.return_value.lanes = ()
        with (
            patch(
                "control_plane.dokploy.cutover_factory.resolve_profile_cutover_plan",
                return_value=fixture.plan,
            ),
            patch("control_plane.dokploy.service_cutover.installed_cutover_host") as load,
            self.assertRaisesRegex(ValueError, "persisted lane/artifact authority"),
        ):
            service_odoo_cutover_factory(store, Path(fixture.temporary.name))(fixture.plan)
        load.assert_not_called()

    def test_default_web_factory_uses_fresh_records_and_refuses_stale_plan_before_host(
        self,
    ) -> None:
        for target in ("compose", "application"):
            with self.subTest(target=target), TemporaryDirectory() as temporary:
                root = Path(temporary)
                store, intent, _ = web_fixture.WebCutoverDeploymentTests().seed(root, target)
                profile = store.read_product_profile_record("fixture")
                lane = profile.lanes[0]
                admitted = compile_web_cutover(
                    store,
                    product=profile.product,
                    lane=lane,
                    artifact_id=intent.candidate.artifact_id,
                    source_git_ref=intent.candidate.source_commit,
                    deploy_reference=intent.new.deploy_reference,
                    record_id=intent.release_id,
                )
                concrete, _, route = web_provider_fixture.WebCutoverProviderTests().provider(
                    root, target
                )
                host: Any = MagicMock()
                host.web.return_value = concrete.driver
                host.route.return_value = route
                provider = default_generic_web_deploy_provider(
                    record_store=store, control_plane_root=root
                )
                factory = getattr(provider, "cutover_provider_factory")
                with (
                    patch(
                        "control_plane.dokploy.service_cutover.installed_cutover_host",
                        return_value=host,
                    ),
                    patch(
                        "control_plane.dokploy.service_cutover.read_dokploy_config",
                        return_value=("https://provider.invalid", "inert"),
                    ),
                ):
                    bound = factory(admitted)
                self.assertEqual(bound.plan, admitted)
                host.web.assert_called_once_with(admitted, store, root)
                # Reload the profile on every use; the old factory cannot keep
                # authority after an admin removes the lane's cutover contract.
                store.write_product_profile_record(
                    profile.model_copy(
                        update={"lanes": (lane.model_copy(update={"generic_web_cutover": None}),)}
                    )
                )
                with (
                    patch("control_plane.dokploy.service_cutover.installed_cutover_host") as load,
                    self.assertRaises(ValueError),
                ):
                    service_web_cutover_factory(store, root)(admitted)
                load.assert_not_called()
                store.write_product_profile_record(profile.model_copy(update={"lanes": ()}))
                with (
                    patch("control_plane.dokploy.service_cutover.installed_cutover_host") as load,
                    self.assertRaisesRegex(ValueError, "persisted lane/artifact authority"),
                ):
                    factory(admitted)
                load.assert_not_called()
