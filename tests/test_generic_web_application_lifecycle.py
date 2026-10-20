from datetime import UTC, datetime
from contextlib import nullcontext
from collections.abc import Callable
from pathlib import Path
from typing import cast
import unittest
from unittest.mock import Mock, patch

import click
from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused

from control_plane.contracts.generic_web_extensions import (
    GenericWebApplicationSchedule,
    GenericWebLaneExtensions,
    GenericWebMaintenanceCommand,
)
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.dokploy import api
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_testing_inputs import TestingInputSafetyRefused
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleSnapshot,
    application_snapshot_for_deploy,
    recorded_application_lifecycle,
)
from control_plane.workflows.generic_web_application_lifecycle import (
    DokployApplicationLifecycle,
    schedule_payload,
    preview_application_lifecycle,
    recover_preview_application_lifecycle,
)
from control_plane.workflows.generic_web_deploy import execute_generic_web_deploy
from control_plane.workflows.generic_web_deploy import GenericWebDeployResult
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
)
from tests.test_generic_web_deploy import (
    _FakeGenericWebDeployProvider,
    _GenericWebDeployStore,
    _profile,
    _request,
)


def _extensions() -> GenericWebLaneExtensions:
    return GenericWebLaneExtensions(
        maintenance=(GenericWebMaintenanceCommand(name="schema-upgrade", command="./migrate"),),
        schedules=(
            GenericWebApplicationSchedule(
                name="billing-recovery", command="./recover-billing", cron_expression="*/15 * * * *"
            ),
        ),
    )


class _LifecycleDeployProvider(_FakeGenericWebDeployProvider, DokployGenericWebDeployProvider):
    provider_id = "dokploy"


class GenericWebApplicationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.extensions = _extensions()
        self.original = schedule_payload(
            GenericWebApplicationSchedule(
                name="billing-recovery",
                command="./previous-recovery",
                cron_expression="*/20 * * * *",
            ),
            "app-123",
            enabled=True,
        )
        self.schedules: dict[str, api.JsonObject] = {"billing-recovery": dict(self.original)}
        self.commands: list[str] = []
        self.phases: list[str] = []
        self.failure_command = ""

    def _read(self, **kwargs: object) -> api.JsonObject | None:
        value = self.schedules.get(str(kwargs["schedule_name"]))
        return dict(value) if value is not None else None

    def _upsert(self, **kwargs: object) -> api.JsonObject:
        payload = cast(api.JsonObject, kwargs["schedule_payload"])
        self.assertEqual(payload["applicationId"], kwargs["application_id"])
        name = str(payload["name"])
        self.schedules[name] = dict(payload)
        return {**payload, "scheduleId": name}

    def _run(self, **kwargs: object) -> None:
        before_trigger = kwargs.get("before_trigger")
        if callable(before_trigger):
            before_trigger()
        payload = self.schedules[str(kwargs["schedule_id"])]
        self.assertFalse(payload["enabled"])
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        command = str(payload["command"])
        self.commands.append(command)
        if command == self.failure_command:
            raise click.ClickException("application command failed")

    def _deploy(
        self,
        *,
        failure_command: str = "",
        verification_fails: bool = False,
        reset: bool = False,
        input_refusal: bool = False,
        before_deployment_records: Callable[[], None] | None = None,
    ) -> tuple[GenericWebDeployResult, _GenericWebDeployStore]:
        self.failure_command = failure_command
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        store = _GenericWebDeployStore(profile)
        provider = _LifecycleDeployProvider(
            target_id="app-123",
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="deployed-123",
                started_at=utc_now_timestamp(),
                finished_at=utc_now_timestamp(),
            ),
        )

        def verification(*_args: object) -> PostDeployUpdateEvidence:
            self.assertEqual(
                self.commands,
                [item.command for item in self.extensions.maintenance if item.on_reset == reset],
            )
            self.assertFalse(self.schedules["billing-recovery"]["enabled"])
            return PostDeployUpdateEvidence(
                attempted=True,
                status="fail" if verification_fails else "pass",
                detail="product check",
            )

        with (
            patch.object(
                provider,
                "execute_artifact_deploy",
                side_effect=TestingInputSafetyRefused("input authority changed"),
            )
            if input_refusal
            else nullcontext(),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.example", "test-token"),
            ),
            patch.object(api, "read_dokploy_application_schedule", side_effect=self._read),
            patch.object(api, "upsert_dokploy_application_schedule", side_effect=self._upsert),
            patch.object(api, "run_dokploy_schedule", side_effect=self._run),
        ):
            result = execute_generic_web_deploy(
                control_plane_root=Path("."),
                record_store=store,
                request=_request().model_copy(update={"reset": reset}),
                deploy_provider=provider,
                post_deploy_executor=verification,
                provider_operation_title="deploy-test",
                provider_effect_checkpoint=self.phases.append,
                before_deployment_records=before_deployment_records,
            )
        return result, store

    def test_source_refusal_before_lifecycle_effects_leaves_no_draft_for_next_attempt(self) -> None:
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        store = _GenericWebDeployStore(profile.model_copy(update={"lanes": (lane,)}))

        def refuse() -> None:
            raise GenericWebPromotionSourceRefused("Testing no longer qualifies.")

        with patch(
            "tests.test_generic_web_application_lifecycle._GenericWebDeployStore",
            return_value=store,
        ):
            for _ in range(2):
                with self.assertRaises(GenericWebPromotionSourceRefused):
                    self._deploy(before_deployment_records=refuse)
                self.assertEqual(store.deployments, [])
                self.assertEqual(self.schedules["billing-recovery"], self.original)
                self.assertEqual(self.commands, [])
            result, _ = self._deploy()
        self.assertEqual(result.deploy_status, "pass")

    def test_input_refusal_after_quiesce_restores_schedules_and_records_failure(self) -> None:
        result, store = self._deploy(input_refusal=True)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIn("application_schedule_quiesce", self.phases)
        self.assertEqual(self.schedules["billing-recovery"], self.original)
        record = store.read_deployment_record(result.deployment_record_id)
        self.assertEqual(record.deploy.status, "fail")
        receipt = recorded_application_lifecycle(record)
        assert receipt is not None
        self.assertEqual(receipt.state, "restored")

    def test_testing_reset_is_explicit_and_uses_the_disposable_lane_guard(self) -> None:
        reset_command = GenericWebMaintenanceCommand(
            name="reset-data", command="./reset-data", on_reset=True
        )
        self.extensions = self.extensions.model_copy(
            update={"maintenance": (*self.extensions.maintenance, reset_command)}
        )
        ordinary, _ = self._deploy()
        self.assertEqual(ordinary.deploy_status, "pass")
        self.assertNotIn(reset_command.command, self.commands)
        self.commands.clear()
        with patch("control_plane.disposable_lanes.require_disposable_lane") as disposable:
            result, _ = self._deploy(reset=True)
        self.assertTrue(disposable.called)
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(self.commands[0], reset_command.command)
        self.assertNotIn(self.extensions.maintenance[0].command, self.commands)
        self.assertTrue(self.schedules["billing-recovery"]["enabled"])

    def _preview_lifecycle(
        self, store: object, extensions: GenericWebLaneExtensions
    ) -> DokployApplicationLifecycle:
        return preview_application_lifecycle(
            host="https://provider.example",
            token="fixture-token",
            application_id="app-123",
            product="sample",
            context="sample-previews",
            instance="pr-42",
            artifact_id="sample-image",
            source_git_ref="a" * 40,
            extensions=extensions,
            record_store=store,
        )

    def test_removing_a_preview_schedule_pauses_then_removes_previously_owned_job(self) -> None:
        self._api_context()
        store = _GenericWebDeployStore(_profile())
        self.schedules["unrelated-job"] = {**self.original, "name": "unrelated-job"}
        first = self._preview_lifecycle(store, self.extensions)
        first.capture()
        first.prepare()
        first.maintain()
        first.complete()
        self.assertTrue(self.schedules["billing-recovery"]["enabled"])
        next_attempt = self._preview_lifecycle(store, GenericWebLaneExtensions())
        next_attempt.capture()
        next_attempt.prepare()
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        next_attempt.complete()
        self.assertNotIn("billing-recovery", self.schedules)
        self.assertTrue(self.schedules["unrelated-job"]["enabled"])

    def test_a_pre_image_retry_cannot_restore_jobs_held_by_an_unqualified_preview(self) -> None:
        self._api_context()
        store = _GenericWebDeployStore(_profile())
        failed = self._preview_lifecycle(store, self.extensions)
        failed.capture()
        failed.prepare()
        recover_preview_application_lifecycle(
            failed, image_effect_attempted=True, target_restored=False
        )
        retried = self._preview_lifecycle(store, self.extensions)
        retried.capture()
        retried.prepare()
        recover_preview_application_lifecycle(
            retried, image_effect_attempted=False, target_restored=True
        )
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])

    def test_preview_with_no_declarations_or_owned_jobs_creates_no_receipt(self) -> None:
        store = _GenericWebDeployStore(_profile())
        lifecycle = self._preview_lifecycle(store, GenericWebLaneExtensions())
        lifecycle.capture()
        lifecycle.prepare()
        lifecycle.complete()
        self.assertEqual(store.deployments, [])

    def test_reset_only_declarations_cannot_silently_target_schedules_or_previews(self) -> None:
        from control_plane.contracts.product_profile_record import ProductPreviewProfile

        with self.assertRaises(ValueError):
            GenericWebApplicationSchedule(
                name="job", command="./job", cron_expression="*/15 * * * *", on_reset=True
            )
        with self.assertRaises(ValueError):
            ProductPreviewProfile(
                generic_web_extensions=GenericWebLaneExtensions(
                    maintenance=(
                        GenericWebMaintenanceCommand(
                            name="reset", command="./reset", on_reset=True
                        ),
                    )
                )
            )

        profile = _profile()
        lane = profile.lanes[0].model_copy(
            update={
                "instance": "prod",
                "generic_web_extensions": GenericWebLaneExtensions(
                    maintenance=(
                        GenericWebMaintenanceCommand(
                            name="reset", command="./reset", on_reset=True
                        ),
                    )
                ),
            }
        )
        with self.assertRaises(ValueError):
            type(profile).model_validate(
                profile.model_copy(update={"lanes": (lane,)}).model_dump(mode="json")
            )

    def test_preview_receipt_storage_failure_cannot_pause_a_job(self) -> None:
        self._api_context()
        store = _GenericWebDeployStore(_profile())
        lifecycle = self._preview_lifecycle(store, self.extensions)
        with patch.object(
            store, "write_deployment_record", side_effect=RuntimeError("storage unavailable")
        ):
            with self.assertRaises(click.ClickException):
                lifecycle.capture()
        self.assertEqual(self.schedules["billing-recovery"], self.original)
        self.assertEqual(self.phases, [])

    def test_production_reset_is_refused_before_any_schedule_mutation(self) -> None:
        self._api_context()
        profile = _profile()
        lane = profile.lanes[0].model_copy(
            update={"instance": "prod", "generic_web_extensions": self.extensions}
        )
        profile = profile.model_copy(update={"lanes": (lane,)})
        provider = _LifecycleDeployProvider(target_id="app-123")
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=_GenericWebDeployStore(profile),
            request=_request().model_copy(update={"instance": "prod", "reset": True}),
            profile=profile,
            lane=lane,
            deploy_provider=provider,
        )
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.schedules["billing-recovery"], self.original)

    def test_reset_refusal_changes_no_schedules(self) -> None:
        self.extensions = self.extensions.model_copy(
            update={
                "maintenance": (
                    GenericWebMaintenanceCommand(
                        name="reset-data", command="./reset-data", on_reset=True
                    ),
                )
            }
        )
        with patch(
            "control_plane.disposable_lanes.require_disposable_lane",
            side_effect=ValueError("staff using testing"),
        ):
            result, _ = self._deploy(reset=True)
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(self.schedules["billing-recovery"], self.original)
        self.assertEqual(self.commands, [])

    def _api_context(self) -> None:
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.example", "test-token"),
            )
        )
        self.enterContext(
            patch.object(api, "read_dokploy_application_schedule", side_effect=self._read)
        )
        self.enterContext(
            patch.object(api, "upsert_dokploy_application_schedule", side_effect=self._upsert)
        )
        self.enterContext(patch.object(api, "run_dokploy_schedule", side_effect=self._run))
        self.enterContext(
            patch.object(
                api,
                "delete_dokploy_application_schedule",
                side_effect=lambda **kwargs: self.schedules.pop(
                    str(cast(api.JsonObject, kwargs["schedule_payload"])["name"]), None
                ),
            )
        )

    def test_interrupted_image_deploy_retains_original_schedule_before_retry(self) -> None:
        self._check_interrupted_retry("interrupted")

    def test_new_attempt_after_interruption_retains_original_enabled_schedule(self) -> None:
        self._check_interrupted_retry("new-attempt")

    def _check_interrupted_retry(self, retry_record_id: str) -> None:
        self._api_context()
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        store = _GenericWebDeployStore(profile)
        provider = _LifecycleDeployProvider(target_id="app-123")

        def interrupt(**kwargs: object) -> None:
            del kwargs
            raise SystemExit("worker interrupted")

        with patch.object(provider, "execute_artifact_deploy", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                execute_generic_web_deploy(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=_request(),
                    deploy_provider=provider,
                    deployment_record_id="interrupted",
                )
        receipt = recorded_application_lifecycle(store.read_deployment_record("interrupted"))
        assert receipt is not None
        self.assertEqual(receipt.originals["billing-recovery"], self.original)
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        self.failure_command = self.extensions.maintenance[0].command
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deploy_provider=provider,
            deployment_record_id=retry_record_id,
        )
        self.assertEqual(result.post_deploy_status, "fail")
        retried = recorded_application_lifecycle(store.read_deployment_record(retry_record_id))
        assert retried is not None
        self.assertEqual(retried.originals, receipt.originals)
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])

    def test_refusal_before_image_mutation_restores_original_enabled_schedule(self) -> None:
        self._api_context()
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        provider = _LifecycleDeployProvider(target_id="app-123")
        with patch.object(
            provider,
            "execute_artifact_deploy",
            side_effect=click.ClickException("image preflight refused"),
        ):
            result = execute_generic_web_deploy(
                control_plane_root=Path("."),
                record_store=_GenericWebDeployStore(profile),
                request=_request(),
                deploy_provider=provider,
            )
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(self.schedules["billing-recovery"], self.original)
        self.assertEqual(self.commands, [])

    def test_unrelated_disabled_maintenance_schedule_is_not_overwritten(self) -> None:
        unrelated = schedule_payload(
            GenericWebMaintenanceCommand(
                name=self.extensions.maintenance[0].name, command="./unrelated"
            ),
            "app-123",
            enabled=False,
        )
        self.schedules[self.extensions.maintenance[0].name] = unrelated
        result, _store = self._deploy()
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.schedules[self.extensions.maintenance[0].name], unrelated)

    def test_profile_edit_cannot_make_existing_attempt_image_only(self) -> None:
        _result, store = self._deploy()
        lane = store.profile.lanes[0].model_copy(
            update={"generic_web_extensions": GenericWebLaneExtensions()}
        )
        profile = store.profile.model_copy(update={"lanes": (lane,)})
        provider = _LifecycleDeployProvider(target_id="app-123")
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=Path("."),
            record_store=store,
            deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
            profile=profile,
            lane=lane,
            trace_id="testing-recovery",
            deploy_provider=provider,
        )
        with patch.object(adapter, "_runtime_close_out_evidence") as close_out:
            self.assertIsNone(
                adapter.inspect_runtime_close_out(
                    resolved_deploy_target=provider.resolve_deploy_target(
                        control_plane_root=Path("."),
                        request_artifact_id=_request().artifact_id,
                        request_source_git_ref=_request().source_git_ref,
                        request_timeout_seconds=None,
                        request_no_cache=False,
                        record_store=store,
                        profile=profile,
                        lane=lane,
                        normalized_artifact_id=store.deployments[0].artifact_identity.artifact_id
                        if store.deployments[0].artifact_identity
                        else "",
                        fallback_target_name="application",
                    )
                )
            )
        close_out.assert_not_called()

        with patch.object(adapter, "_runtime_close_out_evidence", return_value=None) as close_out:
            adapter.inspect_runtime_close_out(
                resolved_deploy_target=provider.resolved_targets[0],
                provider_operation_key="different-operation",
            )
        close_out.assert_called_once()

    def test_changed_declaration_cannot_erase_existing_attempt_receipt(self) -> None:
        _result, store = self._deploy()
        record_id = store.deployments[0].record_id
        original = recorded_application_lifecycle(store.read_deployment_record(record_id))
        lane = store.profile.lanes[0].model_copy(
            update={"generic_web_extensions": GenericWebLaneExtensions()}
        )
        store.profile = store.profile.model_copy(update={"lanes": (lane,)})
        provider = _LifecycleDeployProvider(target_id="app-123")
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deployment_record_id=record_id,
            deploy_provider=provider,
        )
        self.assertEqual(result.deploy_status, "fail")
        self.assertFalse(result.provider_effect_attempted)
        self.assertEqual(provider.runtime_identities, [])
        self.assertEqual(
            recorded_application_lifecycle(store.read_deployment_record(record_id)), original
        )

    def test_removed_declaration_removes_only_previously_managed_schedule(self) -> None:
        clock = self.enterContext(patch("control_plane.workflows.ship.datetime", wraps=datetime))
        clock.now.return_value = datetime.now(UTC)
        result, store = self._deploy()
        first_record_id = result.deployment_record_id
        self.assertEqual(result.post_deploy_status, "pass")
        self._api_context()
        unrelated = {**self.original, "name": "unrelated"}
        self.schedules["unrelated"] = unrelated
        lane = store.profile.lanes[0].model_copy(
            update={"generic_web_extensions": GenericWebLaneExtensions()}
        )
        store.profile = store.profile.model_copy(update={"lanes": (lane,)})
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deploy_provider=_LifecycleDeployProvider(target_id="app-123"),
        )
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertNotEqual(result.deployment_record_id, first_record_id)
        self.assertNotIn("billing-recovery", self.schedules)
        self.assertEqual(self.schedules["unrelated"], unrelated)

    def test_future_declarations_do_not_run_maintenance_on_a_pre_extension_rollback(self) -> None:
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        provider = _LifecycleDeployProvider(target_id="app-123")
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=_GenericWebDeployStore(profile),
            request=_request(),
            deploy_provider=provider,
            rollback=True,
        )
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "skipped")
        self.assertEqual(self.commands, [])
        self.assertEqual(self.schedules["billing-recovery"], self.original)

    def test_older_rollback_uses_the_matching_historical_baseline(self) -> None:
        _result, store = self._deploy()
        first = store.deployments[0]
        receipt = recorded_application_lifecycle(first)
        assert receipt is not None
        receipt.previous_artifact_id = "older-artifact"
        receipt.previous_source_git_ref = "older-source"
        store.write_deployment_record(
            first.model_copy(
                update={
                    "post_deploy_update": first.post_deploy_update.model_copy(
                        update={"evidence": receipt.evidence()}
                    )
                }
            )
        )
        assert first.artifact_identity is not None
        current = ApplicationLifecycleSnapshot(
            product=store.profile.product,
            context=first.context,
            instance=first.instance,
            provider_id="dokploy",
            target_id="app-123",
            artifact_id="current-artifact",
            source_git_ref="current-source",
            previous_artifact_id=first.artifact_identity.artifact_id,
            previous_source_git_ref=first.source_git_ref,
            extensions=self.extensions,
            originals={"billing-recovery": self.schedules["billing-recovery"]},
            managed_names=("billing-recovery",),
            state="qualified",
        )
        second = first.model_copy(
            update={
                "record_id": "current",
                "artifact_identity": first.artifact_identity.model_copy(
                    update={"artifact_id": current.artifact_id}
                ),
                "source_git_ref": current.source_git_ref,
                "post_deploy_update": first.post_deploy_update.model_copy(
                    update={"evidence": current.evidence()}
                ),
            }
        )
        store.write_deployment_record(second)
        inventory = store.inventories[0].model_copy(
            update={
                "deployment_record_id": second.record_id,
                "artifact_identity": second.artifact_identity,
                "source_git_ref": second.source_git_ref,
            }
        )
        store.write_environment_inventory(inventory)
        snapshot = application_snapshot_for_deploy(
            store=store,
            product=store.profile.product,
            context=first.context,
            instance=first.instance,
            provider_id="dokploy",
            target_id="app-123",
            artifact_id=receipt.previous_artifact_id,
            source_git_ref=receipt.previous_source_git_ref,
            deployment_record_id="older-rollback",
            extensions=self.extensions,
            rollback=True,
        )
        assert snapshot is not None
        self.assertEqual(snapshot.originals["billing-recovery"], self.original)
        self.assertNotEqual(
            snapshot.originals["billing-recovery"], current.originals["billing-recovery"]
        )

    def test_declared_lifecycle_runs_on_shared_driver_in_order(self) -> None:
        result, store = self._deploy()
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(
            self.commands,
            [self.extensions.maintenance[0].command, self.extensions.schedules[0].command],
        )
        self.assertLess(
            self.phases.index("application_schedule_quiesce"), self.phases.index("deploy_trigger")
        )
        self.assertTrue(self.schedules["billing-recovery"]["enabled"])
        self.assertEqual(store.inventories[0].post_deploy_update.status, "pass")
        receipt = recorded_application_lifecycle(store.deployments[0])
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.state, "qualified")

    def test_migration_failure_cannot_run_billing_or_qualify_lane(self) -> None:
        result, store = self._deploy(failure_command=self.extensions.maintenance[0].command)
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(self.commands, [self.extensions.maintenance[0].command])
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        self.assertEqual(store.inventories[0].post_deploy_update.status, "fail")

    def test_canary_failure_keeps_schedule_paused(self) -> None:
        result, _store = self._deploy(failure_command=self.extensions.schedules[0].command)
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        self.assertNotIn("application_schedule_activate", self.phases)

    def test_unreadable_baseline_does_not_checkpoint_canary_or_maintenance_execution(self) -> None:
        for maintenance in (False, True):
            with self.subTest(maintenance=maintenance):
                phases: list[str] = []
                lifecycle = DokployApplicationLifecycle(
                    "https://provider.invalid",
                    "fixture",
                    "app-123",
                    ApplicationLifecycleSnapshot(
                        product="example",
                        context="testing",
                        instance="testing",
                        provider_id="dokploy",
                        target_id="app-123",
                        artifact_id="artifact",
                        source_git_ref="source",
                        extensions=self.extensions,
                    ),
                    phases.append,
                    lambda: None,
                )
                with (
                    patch.object(api, "read_dokploy_application_schedule", return_value=None),
                    patch.object(
                        api,
                        "upsert_dokploy_application_schedule",
                        return_value={"scheduleId": "fixture"},
                    ),
                    patch.object(api, "dokploy_request", return_value=None) as provider,
                ):
                    with self.assertRaises(api.DokployScheduleExecutionFailed) as caught:
                        if maintenance:
                            lifecycle._run_command(self.extensions.maintenance[0])
                        else:
                            lifecycle._run_schedule({"scheduleId": "fixture"}, 60)
                self.assertEqual(caught.exception.cause, "trigger_baseline_unavailable")
                self.assertNotIn("application_schedule_canary", phases)
                self.assertNotIn("application_maintenance_execute", phases)
                self.assertEqual(lifecycle.effect_attempted, maintenance)
                self.assertTrue(
                    all(
                        call.kwargs["path"] == "/api/deployment.allByType"
                        for call in provider.call_args_list
                    )
                )

    def test_product_verification_failure_keeps_new_schedule_unactivated(self) -> None:
        result, _store = self._deploy(verification_fails=True)
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(self.commands, [self.extensions.maintenance[0].command])
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])

    def test_lost_effect_fence_does_not_attempt_restoration(self) -> None:
        def lost_fence(_phase: str) -> None:
            raise RuntimeError("lost lease")

        lifecycle = DokployApplicationLifecycle(
            "https://provider.example",
            "test-token",
            "app-123",
            ApplicationLifecycleSnapshot(
                product="example",
                context="testing",
                instance="testing",
                provider_id="dokploy",
                target_id="app-123",
                artifact_id="artifact",
                source_git_ref="source",
                extensions=self.extensions,
                originals={"billing-recovery": self.original},
            ),
            lost_fence,
            lambda: None,
        )
        with patch.object(api, "upsert_dokploy_application_schedule") as upsert:
            with self.assertRaisesRegex(RuntimeError, "lost lease"):
                lifecycle.restore()
        upsert.assert_not_called()

    def test_recovery_does_not_adopt_image_success_without_lifecycle_receipt(self) -> None:
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        store = _GenericWebDeployStore(profile)
        provider = _LifecycleDeployProvider(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="deployed-123",
                started_at=utc_now_timestamp(),
                finished_at=utc_now_timestamp(),
            )
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=Path("."),
            record_store=store,
            deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
            profile=profile,
            lane=lane,
            trace_id="testing-recovery",
            deploy_provider=provider,
        )
        observation = adapter.observe(
            "provider-operation:lifecycle",
            "application_schedule_canary",
            adapter.reconciliation_key(),
        )
        self.assertEqual(observation.outcome, "unknown")
        self.assertEqual(store.deployments, [])
        with patch.object(adapter, "_runtime_close_out_evidence") as close_out:
            self.assertIsNone(
                adapter.inspect_runtime_close_out(
                    resolved_deploy_target=provider.resolved_targets[0]
                )
            )
        close_out.assert_not_called()

    def test_declared_product_verification_cannot_be_skipped_or_requested_in_production(
        self,
    ) -> None:
        extensions = GenericWebLaneExtensions(verification_extension="verireel-testing")
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        provider = _LifecycleDeployProvider(target_id="app-123")
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=_GenericWebDeployStore(profile),
            request=_request(),
            deploy_provider=provider,
        )
        self.assertEqual(result.deploy_status, "fail")
        self.assertFalse(result.provider_effect_attempted)
        self.assertEqual(provider.runtime_identities, [])
        for change in (
            {"driver_id": "verireel"},
            {"lanes": (lane.model_copy(update={"instance": "prod"}),)},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                type(profile).model_validate(
                    profile.model_copy(update=change).model_dump(mode="json")
                )

    def test_adapter_recovers_a_fully_restored_pre_image_refusal_by_retrying_original_operation(
        self,
    ) -> None:
        from control_plane.provider_operations import (
            ProviderOperationLease,
            ProviderMutationUnknownError,
        )

        self._api_context()
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        store = _GenericWebDeployStore(profile)
        provider = _LifecycleDeployProvider(
            target_id="app-123",
            observation=GenericWebProviderDeploymentObservation(outcome="absent"),
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=Path("."),
            record_store=store,
            deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
            profile=profile,
            lane=lane,
            trace_id="pre-image-refusal",
            deploy_provider=provider,
        )
        operation = "restored-before-image"
        with patch.object(
            provider,
            "execute_artifact_deploy",
            side_effect=click.ClickException("preflight refused"),
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply(operation, Mock(spec=ProviderOperationLease))
        self.assertEqual(self.schedules["billing-recovery"], self.original)
        inspection = adapter.inspect(
            provider_operation_key=operation,
            provider_effect_phase="application_schedule_restore",
            reconciliation_key=adapter.reconciliation_key(),
        )
        self.assertEqual(inspection.observation.outcome, "absent")
        self.assertTrue(inspection.retry_safe)
        self.assertFalse(
            adapter.inspect(
                provider_operation_key="different-attempt",
                provider_effect_phase="application_schedule_restore",
                reconciliation_key=adapter.reconciliation_key(),
            ).retry_safe
        )

        provider.observation = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="retried",
            started_at=utc_now_timestamp(),
            finished_at=utc_now_timestamp(),
        )
        outcome = adapter.apply(operation, Mock(spec=ProviderOperationLease))
        self.assertTrue(outcome.durable)
        self.assertEqual(
            cast(dict[str, object], outcome.response_payload["result"])["deploy_status"], "pass"
        )

    def test_adapter_resumes_an_interruption_after_quiescence_before_any_image_effect(self) -> None:
        self._check_quiesce_resume(refuse=False)

    def test_adapter_restores_jobs_when_a_proved_pre_image_resume_is_refused(self) -> None:
        self._check_quiesce_resume(refuse=True)

    def _check_quiesce_resume(self, *, refuse: bool) -> None:
        from control_plane.provider_operations import ProviderOperationLease

        self._api_context()
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        store = _GenericWebDeployStore(profile)
        provider = _LifecycleDeployProvider(
            target_id="app-123",
            observation=GenericWebProviderDeploymentObservation(outcome="absent"),
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=Path("."),
            record_store=store,
            deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
            profile=profile,
            lane=lane,
            trace_id="paused-before-image",
            deploy_provider=provider,
        )
        operation = "paused-before-image"
        with patch.object(
            provider, "execute_artifact_deploy", side_effect=SystemExit("interrupted")
        ):
            with self.assertRaises(SystemExit):
                adapter.apply(operation, Mock(spec=ProviderOperationLease))
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        inspection = adapter.inspect(
            provider_operation_key=operation,
            provider_effect_phase="application_schedule_quiesce",
            reconciliation_key=adapter.reconciliation_key(),
        )
        self.assertTrue(inspection.retry_safe)
        self.assertFalse(
            adapter.inspect(
                provider_operation_key=operation,
                provider_effect_phase="application_maintenance_execute",
                reconciliation_key=adapter.reconciliation_key(),
            ).retry_safe
        )
        if refuse:
            self.assertTrue(
                adapter.inspect(
                    provider_operation_key=operation,
                    provider_effect_phase="application_schedule_quiesce",
                    reconciliation_key=adapter.reconciliation_key(),
                ).retry_safe
            )
            from control_plane.provider_operations import ProviderMutationUnknownError

            with patch.object(
                provider,
                "execute_artifact_deploy",
                side_effect=click.ClickException("preflight refused"),
            ):
                with self.assertRaises(ProviderMutationUnknownError):
                    adapter.apply(operation, Mock(spec=ProviderOperationLease))
            self.assertEqual(self.schedules["billing-recovery"], self.original)
            self.assertTrue(
                adapter.inspect(
                    provider_operation_key=operation,
                    provider_effect_phase="application_schedule_restore",
                    reconciliation_key=adapter.reconciliation_key(),
                ).retry_safe
            )
            return
        provider.observation = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="resumed",
            started_at=utc_now_timestamp(),
            finished_at=utc_now_timestamp(),
        )
        self.assertTrue(adapter.apply(operation, Mock(spec=ProviderOperationLease)).durable)
        self.assertTrue(self.schedules["billing-recovery"]["enabled"])

    def test_receipt_storage_failure_does_not_mark_a_provider_effect_or_pause_jobs(self) -> None:
        from control_plane.provider_operations import ProviderOperationLease
        from sqlalchemy.exc import SQLAlchemyError

        self._api_context()
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        store = _GenericWebDeployStore(profile)
        provider = _LifecycleDeployProvider(
            target_id="app-123",
            observation=GenericWebProviderDeploymentObservation(outcome="absent"),
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=Path("."),
            record_store=store,
            deploy_request=GenericWebDeployEnvelope(product=profile.product, deploy=_request()),
            profile=profile,
            lane=lane,
            trace_id="receipt-write-failed",
            deploy_provider=provider,
        )
        lease = Mock(spec=ProviderOperationLease)
        with patch.object(
            store, "write_deployment_record", side_effect=SQLAlchemyError("unavailable")
        ):
            with self.assertRaises(SQLAlchemyError):
                adapter.apply("receipt-write-failed", lease)
        lease.checkpoint_effect.assert_not_called()
        self.assertEqual(self.schedules["billing-recovery"], self.original)
        self.assertTrue(
            adapter.inspect(
                provider_operation_key="receipt-write-failed",
                provider_effect_phase="",
                reconciliation_key=adapter.reconciliation_key(),
            ).retry_safe
        )

    def test_unsupported_lifecycle_provider_refuses_before_deploy(self) -> None:
        profile = _profile()
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        profile = profile.model_copy(update={"lanes": (lane,)})
        provider = _FakeGenericWebDeployProvider()
        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=_GenericWebDeployStore(profile),
            request=_request(),
            deploy_provider=provider,
        )
        self.assertEqual(result.deploy_status, "fail")
        self.assertFalse(result.provider_effect_attempted)
        self.assertEqual(provider.runtime_identities, [])

    def test_malformed_cron_member_cannot_hide_behind_a_valid_match(self) -> None:
        with self.assertRaises(ValueError):
            GenericWebApplicationSchedule(
                name="recovery", command="./recover", cron_expression="0,invalid * * * *"
            )

    def test_profile_roundtrip_retains_commands_without_inventing_defaults(self) -> None:
        profile = _profile()
        legacy_payload = profile.model_dump(mode="json")
        self.assertNotIn("generic_web_extensions", legacy_payload["lanes"][0])
        lane = profile.lanes[0].model_copy(update={"generic_web_extensions": self.extensions})
        declared = profile.model_copy(update={"lanes": (lane,)})
        restored = type(profile).model_validate(declared.model_dump(mode="json"))
        self.assertEqual(restored.lanes[0].generic_web_extensions, self.extensions)
        with self.assertRaises(ValueError):
            GenericWebLaneExtensions(
                maintenance=(GenericWebMaintenanceCommand(name="same", command="./migrate"),),
                schedules=(
                    GenericWebApplicationSchedule(
                        name="same", command="./cron", cron_expression="* * * * *"
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
