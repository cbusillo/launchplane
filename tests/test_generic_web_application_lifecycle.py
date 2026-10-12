from pathlib import Path
from typing import cast
import unittest
from unittest.mock import patch

import click

from control_plane.contracts.generic_web_extensions import (
    GenericWebApplicationSchedule,
    GenericWebLaneExtensions,
    GenericWebMaintenanceCommand,
)
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.dokploy import api
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleSnapshot,
    recorded_application_lifecycle,
)
from control_plane.workflows.generic_web_application_lifecycle import (
    DokployApplicationLifecycle,
    schedule_payload,
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
        payload = self.schedules[str(kwargs["schedule_id"])]
        self.assertFalse(payload["enabled"])
        self.assertFalse(self.schedules["billing-recovery"]["enabled"])
        command = str(payload["command"])
        self.commands.append(command)
        if command == self.failure_command:
            raise click.ClickException("application command failed")

    def _deploy(
        self, *, failure_command: str = "", verification_fails: bool = False
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
            self.assertEqual(self.commands, [self.extensions.maintenance[0].command])
            self.assertFalse(self.schedules["billing-recovery"]["enabled"])
            return PostDeployUpdateEvidence(
                attempted=True,
                status="fail" if verification_fails else "pass",
                detail="product check",
            )

        with (
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
                request=_request(),
                deploy_provider=provider,
                post_deploy_executor=verification,
                provider_operation_title="deploy-test",
                provider_effect_checkpoint=self.phases.append,
            )
        return result, store

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

    def test_removed_declaration_removes_only_previously_managed_schedule(self) -> None:
        result, store = self._deploy()
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
        self.assertNotIn("billing-recovery", self.schedules)
        self.assertEqual(self.schedules["unrelated"], unrelated)

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
