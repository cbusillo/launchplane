from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from dataclasses import replace
import unittest
from unittest.mock import patch

from control_plane.contracts.product_profile_record import ProductGenericWebTestingState
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.dokploy import api, post_deploy
from control_plane import testing_application_activation as activation
from control_plane.testing_rebuild_service import NativeTestingRebuildPending
from control_plane.testing_rebuild_driver import TestingRebuildRefused
from control_plane.testing_separation_resources import SeparationResourcesAdapter
from control_plane import testing_separation_resources as resources_module
from control_plane.testing_application_capabilities import RETAINED_CAPABILITY_PROGRAM_NAME
from control_plane.service_auth import BearerIdentityConfig
from tests import test_generic_web_testing_rebuild as flows


def capture_preparation_program(fixture: Any) -> dict[str, str]:
    markers: dict[str, str] = {}
    run = fixture.flow.host_schedule

    def program(**kwargs: Any) -> dict[str, str]:
        result: dict[str, str] = run(**kwargs)
        identifier = "captured-capability"
        fixture.flow.schedules[identifier] = {
            **kwargs["schedule_payload"],
            "scheduleId": identifier,
        }
        fixture.flow.schedule_history[identifier] = [
            {
                "scheduleId": identifier,
                "deploymentId": "captured-execution",
                "status": "done",
                "startedAt": fixture.flow.old.now(-1),
                "finishedAt": fixture.flow.old.now(),
            }
        ]
        markers.update(result, log_available="true")
        return result

    fixture.enterContext(patch.object(post_deploy, "run_manual_data_schedule", side_effect=program))
    fixture.enterContext(
        patch.object(
            post_deploy,
            "_read_odoo_post_deploy_log_markers",
            side_effect=lambda **_kwargs: dict(markers),
        )
    )

    return markers


def interrupt_original_preparation(fixture: Any, change: Any) -> dict[str, str]:
    markers = capture_preparation_program(fixture)
    changed = False

    def at_storage(phase: str) -> None:
        nonlocal changed
        if phase == "resources:fresh-storage" and not changed:
            changed = True
            change()

    fixture.authorize.side_effect = at_storage
    with fixture.assertRaises(NativeTestingRebuildPending):
        fixture.execute(1)
    fixture.assertTrue(changed)
    return markers


def assert_original_preparation_refusal(fixture: Any, change: Any) -> None:
    target_id = fixture.store.read_dokploy_target_id_record(
        context_name=fixture.flow.context, instance_name="testing"
    ).target_id
    projects = deepcopy(fixture.flow.projects)
    interrupt_original_preparation(fixture, change)
    profile = fixture.store.read_product_profile_record(fixture.flow.product)
    before = dict(fixture.flow.storage.volumes)
    with patch.object(
        fixture.store,
        "_database_mutation_timestamp",
        return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    ):
        result = fixture.execute(1)
    fixture.assertEqual(result.deploy_status, "fail")
    fixture.assertEqual(fixture.store.read_product_profile_record(fixture.flow.product), profile)
    fixture.assertEqual(result.target_id, target_id)
    fixture.assertEqual(fixture.flow.storage.volumes, before)
    fixture.assertEqual(fixture.flow.projects, projects)
    fixture.assertIsNone(
        fixture.store.read_dokploy_target_record(
            context_name=fixture.flow.context, instance_name="testing"
        ).policies.staff_testing_hold
    )


class GenericWebTestingRebuildRecoveryTests(flows.GenericWebTestingRebuildFlowFixture):
    def hold(self) -> Any:
        return self.store.read_dokploy_target_record(
            context_name=self.flow.context, instance_name="testing"
        ).policies.staff_testing_hold

    def change_profile(self, *, state: bool = False) -> None:
        profile = self.store.read_product_profile_record(self.flow.product)
        if state:
            next(
                lane for lane in profile.lanes if lane.instance == "testing"
            ).generic_web_testing_state = ProductGenericWebTestingState(
                kind="volume", container_path="/data"
            )
        else:
            profile.display_name = "An updated product name"
        self.store.write_product_profile_record(profile)

    def test_original_capability_settles_refusal_after_desired_state_changes(self) -> None:
        path = self.flow.old_backing / "keep"
        before = path.read_text()
        assert_original_preparation_refusal(self, lambda: self.change_profile(state=True))
        self.assertEqual(path.read_text(), before)

    def test_unproven_original_capability_keeps_the_original_key_and_hold(self) -> None:
        for fault in ("active", "ambiguous", "unreadable"):
            with self.subTest(fault=fault):
                case = GenericWebTestingRebuildRecoveryTests()
                case.setUp()
                try:
                    markers = interrupt_original_preparation(
                        case, lambda: case.change_profile(state=True)
                    )
                    program = next(
                        row
                        for row in case.flow.schedules.values()
                        if row["name"] == RETAINED_CAPABILITY_PROGRAM_NAME
                    )
                    history = case.flow.schedule_history[program["scheduleId"]][0]
                    if fault == "active":
                        history["status"] = "running"
                    elif fault == "ambiguous":
                        case.flow.schedules["duplicate"] = {**program, "scheduleId": "duplicate"}
                    else:
                        markers["log_available"] = "false"
                    with patch.object(
                        case.store,
                        "_database_mutation_timestamp",
                        return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    ):
                        with case.assertRaises(NativeTestingRebuildPending):
                            case.execute(1)
                        case.assertIsNotNone(case.hold())
                        case.assertEqual(case.flow.projects, [])
                        case.assertEqual(case.flow.storage.volumes, {})
                        history["status"] = "done"
                        case.flow.schedules.pop("duplicate", None)
                        markers["log_available"] = "true"
                        result = case.execute(1)
                    case.assertEqual(result.deploy_status, "fail")
                    case.assertIsNone(case.hold())
                finally:
                    case.doCleanups()

    def test_original_completed_storage_is_read_back_after_state_changes_without_allocation(
        self,
    ) -> None:
        run = resources_module.run_preparation_schedule
        calls: list[str] = []

        def storage(**kwargs: Any) -> Any:
            mode = kwargs.get("mode", "prepare")
            calls.append(mode)
            result = run(**kwargs)
            if mode == "prepare":
                self.change_profile(state=True)
                raise OSError("Lost completed preparation response")
            return result

        with patch.object(resources_module, "run_preparation_schedule", side_effect=storage):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
            profile = self.store.read_product_profile_record(self.flow.product)
            with patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            ):
                result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.store.read_product_profile_record(self.flow.product), profile)
        self.assertEqual(calls.count("prepare"), 1)
        self.assertIn("readback", calls)
        self.assertEqual(self.flow.projects, [])

    def test_changed_preparation_binding_finishes_failed_without_stranding_hold(self) -> None:
        def changed(phase: str) -> None:
            if phase == "resources:admission":
                self.change_profile()

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())

    def test_state_change_after_own_hold_before_child_dispatch_finishes_failed(self) -> None:
        def changed(phase: str) -> None:
            if phase == "resources:admission":
                self.change_profile(state=True)

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.projects, [])
        self.assertEqual(self.deleted, [])

    def test_changed_admission_inside_reserved_child_releases_only_undispatched_child(self) -> None:
        admissions = 0

        def changed(phase: str) -> None:
            nonlocal admissions
            if phase == "resources:admission":
                admissions += 1
                if admissions == 2:
                    self.change_profile(state=True)

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.projects, [])
        self.assertEqual(self.deleted, [])

    def test_published_provider_binding_drift_never_retires_the_original(self) -> None:
        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                provider = self.store.read_provider_target_record(
                    context_name=self.flow.context, instance_name="testing"
                )
                self.store.write_provider_target_record(
                    provider.model_copy(update={"provider_id": "other-provider"})
                )

        self.authorize.side_effect = changed
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(1)
        self.assertIsNotNone(self.hold())
        self.assertEqual(self.deleted, [])

    def test_state_change_after_publication_does_not_strand_finalization(self) -> None:
        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                self.change_profile(state=True)

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [self.flow.old_id])

    def test_external_predecessor_deletion_settles_only_positive_absence(self) -> None:
        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization" and self.flow.old_id in self.flow.payloads:
                self.flow.payloads.pop(self.flow.old_id)
                self.remove_runtime("old-app")

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [])

    def test_unknown_child_effect_keeps_own_hold_and_original_key(self) -> None:
        def unknown(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("Lost original preparation response")

        with patch.object(SeparationResourcesAdapter, "apply", side_effect=unknown):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
        self.assertIsNotNone(self.hold())
        self.assertEqual(self.flow.projects, [])
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(1)
        self.assertEqual(result.health_status, "pass")

    def test_published_recovery_uses_sealed_environment_after_desired_settings_change(self) -> None:
        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                self.store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context=self.flow.context,
                        instance="testing",
                        env={"NEW_SETTING": "next-deployment"},
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                )

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertIsNone(self.hold())
        self.assertNotIn("NEW_SETTING", self.flow.payloads[result.target_id]["env"])

    def test_published_recovery_refuses_actual_provider_environment_drift(self) -> None:
        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                target = self.store.read_dokploy_target_id_record(
                    context_name=self.flow.context, instance_name="testing"
                )
                self.flow.payloads[target.target_id]["env"] += "\nFOREIGN_SETTING=changed"

        self.authorize.side_effect = changed
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(1)
        self.assertIsNotNone(self.hold())
        self.assertEqual(self.deleted, [])

    def test_same_commit_image_change_before_recipient_dispatch_finishes_failed(self) -> None:
        with patch.object(
            activation,
            "verified_product_image",
            return_value="ghcr.io/example/site@sha256:" + "b" * 64,
        ):
            result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.projects, [])
        self.assertEqual(self.deleted, [])

    def test_enabled_predecessor_schedule_refuses_before_own_hold(self) -> None:
        request = api.dokploy_request

        def schedules(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/schedule.list":
                return [{"scheduleId": "cron", "applicationId": self.flow.old_id, "enabled": True}]
            return request(**kwargs)

        with patch.object(api, "dokploy_request", side_effect=schedules):
            result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.projects, [])

    def test_predecessor_preview_refuses_before_own_hold(self) -> None:
        self.flow.payloads[self.flow.old_id]["previewDeployments"] = [{"id": "preview"}]
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.projects, [])

    def test_external_metadata_delete_with_surviving_runtime_reports_unverified_retirement(
        self,
    ) -> None:
        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                self.flow.payloads.pop(self.flow.old_id, None)

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertIn(self.flow.old_id, result.runtime_source["retained_predecessors"])
        self.assertIsNone(self.hold())
        self.assertIn("old-app", self.flow.services)
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.flow.normal("after-external-retirement-leftover").status, "completed")

    def test_static_change_cannot_settle_an_unknown_original_delete_as_a_leftover(self) -> None:
        with patch.object(
            api, "delete_dokploy_application", side_effect=OSError("Unanswered delete")
        ):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
        self.flow.payloads[self.flow.old_id]["previewDeployments"] = [{"id": "preview"}]
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            with self.assertRaises(NativeTestingRebuildPending) as pending:
                self.execute(1)
            self.assertIsNotNone(self.hold())
            self.assertEqual(self.deleted, [])
            self.assertIsInstance(pending.exception.__cause__, TestingRebuildRefused)
            self.assertIn(str(pending.exception.__cause__), str(pending.exception))
            self.flow.payloads[self.flow.old_id]["previewDeployments"] = []
            self.assertEqual(self.execute(1).post_deploy_status, "pass")
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [self.flow.old_id])

    def test_static_change_waits_for_original_deploy_disable_before_reporting_a_leftover(
        self,
    ) -> None:
        request = api.dokploy_request

        def unanswered(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/application.update" and kwargs.get("payload") == {
                "applicationId": self.flow.old_id,
                "autoDeploy": False,
            }:
                raise OSError("Unanswered automatic deployment edit")
            return request(**kwargs)

        with patch.object(api, "dokploy_request", side_effect=unanswered):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
        self.flow.payloads[self.flow.old_id]["previewDeployments"] = [{"id": "preview"}]
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            with self.assertRaises(NativeTestingRebuildPending) as pending:
                self.execute(1)
            self.assertIsNotNone(self.hold())
            self.assertTrue(self.flow.payloads[self.flow.old_id]["autoDeploy"])
            self.assertIsInstance(pending.exception.__cause__, TestingRebuildRefused)
            self.assertIn(str(pending.exception.__cause__), str(pending.exception))
            self.flow.payloads[self.flow.old_id]["autoDeploy"] = False
            result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [])

    def test_changed_contract_after_private_creation_uses_existing_candidate_refusal(self) -> None:
        def changed(phase: str) -> None:
            if phase == "activation:deploy_application_deployment_id":
                self.change_profile(state=True)

        self.authorize.side_effect = changed
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [])
        self.assertEqual(result.target_id, self.flow.old_id)
        self.assertEqual(len(self.flow.projects), 1)

    def test_external_metadata_absence_settles_an_unanswered_deploy_disable(self) -> None:
        request = api.dokploy_request

        def unanswered(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/application.update" and kwargs.get("payload") == {
                "applicationId": self.flow.old_id,
                "autoDeploy": False,
            }:
                raise OSError("Unanswered automatic deployment edit")
            return request(**kwargs)

        with patch.object(api, "dokploy_request", side_effect=unanswered):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
        self.remove_provider_metadata(self.flow.old_id)
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertIsNone(self.hold())
        self.assertIn("old-app", self.flow.services)
        self.assertEqual(self.deleted, [])

    def test_real_worker_recovers_published_original_after_state_change(self) -> None:
        from control_plane import service_agent_identities, service_bootstrap
        from control_plane.workflows import odoo_stable_operation_worker as worker

        operation = pause_published(self)
        self.change_profile(state=True)
        now = datetime.now(UTC)
        operation = operation.model_copy(
            update={
                "status": "running",
                "phase": "running",
                "lease_owner": "worker",
                "started_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        config = caller_config(self)
        with (
            patch.object(
                service_agent_identities, "load_service_agent_identities", return_value=config
            ),
            patch.object(
                service_bootstrap, "_bearer_identity_config_from_env", return_value=config
            ),
        ):
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.flow.root,
                    operation=operation,
                    lease_owner="worker",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        completed = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(completed.status, "pass", completed.error_message)
        self.assertIsNone(self.hold())

    def test_original_recovery_refuses_replaced_or_non_director_caller(self) -> None:
        from control_plane.service_auth import configured_local_operator_identity
        from control_plane.testing_rebuild_authorization import (
            director_testing_rebuild_recovery_allowed,
        )

        operation = pause_published(self)
        self.change_profile(state=True)
        identity = configured_local_operator_identity(caller_config(self))
        assert identity is not None
        self.assertTrue(
            director_testing_rebuild_recovery_allowed(
                identity=identity, operation=operation, store=self.store
            )
        )
        for caller in (
            replace(identity, token_label="replacement"),
            replace(identity, director_agent=False),
        ):
            with self.subTest(caller=caller):
                self.assertFalse(
                    director_testing_rebuild_recovery_allowed(
                        identity=caller, operation=operation, store=self.store
                    )
                )
        self.assertEqual(self.deleted, [])


def caller_config(test: Any) -> BearerIdentityConfig:
    return BearerIdentityConfig(
        local_operator_token="original-operator-token",
        local_operator_subject=test.flow.caller_identity.subject,
        local_operator_token_label=test.flow.caller_identity.token_label,
    )


def pause_published(test: Any) -> Any:
    def pause(phase: str) -> None:
        if phase == "testing_rebuild_finalization":
            raise NativeTestingRebuildPending("Observe original finalization")

    test.authorize.side_effect = pause
    with test.assertRaises(NativeTestingRebuildPending):
        test.execute(1)
    test.authorize.side_effect = None
    return test.store.read_odoo_stable_target_replacement_operation_record("generic-cycle-1")


class GenericWebTestingRebuildHttpRecoveryTests(
    flows.GenericWebTestingRebuildFlowFixture, unittest.IsolatedAsyncioTestCase
):
    async def test_original_director_can_resume_after_forward_state_is_no_longer_supported(
        self,
    ) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _RejectingVerifier
        from tests import test_odoo_prod_promotion_operation as administrator
        from tests.support.http import request

        original = pause_published(self)
        profile = self.store.read_product_profile_record(self.flow.product)
        next(
            lane for lane in profile.lanes if lane.instance == "testing"
        ).generic_web_testing_state = ProductGenericWebTestingState(
            kind="volume", container_path="/data"
        )
        self.store.write_product_profile_record(profile)
        held = original.model_copy(
            update={
                "status": "reconciliation_required",
                "error_code": "operation_authorization_testing_rebuild_refused",
                "error_message": "Original caller authority needs refresh",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(held)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=administrator._ADMINISTRATOR_POLICY,
            bearer_identity_config=caller_config(self),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.flow.root,
        )
        response = await request(
            app,
            "POST",
            f"/v1/products/{held.product}/testing-rebuild/operations/{held.operation_id}/resume",
            headers={
                "Authorization": "Bearer original-operator-token",
                "Idempotency-Key": held.idempotency_key,
            },
            payload={"expected_updated_at": held.updated_at, "expected_attempt": held.attempt},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation"]["operation_id"], held.operation_id)
        self.assertEqual(self.deleted, [])
