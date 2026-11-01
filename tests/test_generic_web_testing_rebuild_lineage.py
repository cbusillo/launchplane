from datetime import UTC, datetime, timedelta
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from control_plane import generic_web_testing_rebuild as rebuild
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy import api
from control_plane.testing_application_runtime import inspect_application_state
from control_plane.testing_lane_hold import apply_testing_hold_plan
from control_plane.testing_rebuild_service import NativeTestingRebuildPending
from tests import test_generic_web_testing_rebuild as generic


class PredecessorLineageTests(generic.GenericWebTestingRebuildFlowFixture):
    def hold(self) -> Any:
        return self.store.read_dokploy_target_record(
            context_name=self.flow.context, instance_name="testing"
        ).policies.staff_testing_hold

    def retain_original(self) -> str:
        read = rebuild.GenericWebRebuildActivationAdapter.read_edge_health

        def failed(adapter: Any, record: Any, **kwargs: Any) -> Any:
            if record.published:
                return HealthcheckEvidence(
                    verified=True,
                    status="fail",
                    urls=("https://testing.example.invalid/health",),
                    timeout_seconds=5,
                    runtime_identity_status="match",
                    observed_runtime_identity=kwargs.get("expected"),
                )
            return read(adapter, record, **kwargs)

        with patch.object(
            rebuild.GenericWebRebuildActivationAdapter, "read_edge_health", new=failed
        ):
            result = self.execute(1)
        self.assertEqual(result.health_status, "fail")
        self.retained_predecessors.add(self.flow.old_id)
        self.assertEqual(self.flow.normal("repair-before-retirement").status, "completed")
        target_id = result.target_id
        assert isinstance(target_id, str)
        return target_id

    def test_lost_ancestor_delete_reply_keeps_its_own_runtime_removal_authority(self) -> None:
        previous = self.retain_original()
        self.keep_predecessor_service = True
        delete = api.delete_dokploy_application

        def lost(**kwargs: Any) -> None:
            delete(**kwargs)
            if kwargs["application_id"] == self.flow.old_id:
                raise OSError("Lost ancestor delete reply")

        with patch.object(api, "delete_dokploy_application", side_effect=lost):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(2)
        self.assertNotIn(self.flow.old_id, self.flow.payloads)
        self.assertIn("old-app", self.flow.services)
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertNotIn("old-app", self.flow.services)
        self.assertEqual(self.deleted, [previous, self.flow.old_id])
        self.assertEqual(self.flow.normal("after-ancestor-retirement").status, "completed")

    def test_provider_only_reference_protects_an_ancestor_from_retirement(self) -> None:
        self.retain_original()
        provider = self.store.read_provider_target_record(
            context_name=self.flow.context, instance_name="testing"
        )
        alias = provider.model_copy(
            update={"context": "another-lane", "target_id": self.flow.old_id}
        )
        self.store.write_provider_target_record(alias)
        result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertIsNone(self.hold())
        self.assertIn(self.flow.old_id, self.flow.payloads)
        self.assertNotIn(self.flow.old_id, self.deleted)
        self.assertEqual(
            self.store.read_provider_target_record(
                context_name=alias.context, instance_name=alias.instance
            ),
            alias,
        )
        self.assertEqual(self.flow.normal("after-protected-ancestor").status, "completed")

    def test_third_rebuild_does_not_contact_a_previously_retired_ancestor_host(self) -> None:
        self.assertEqual(self.execute(1).health_status, "pass")
        self.assertEqual(self.execute(2).health_status, "pass")
        observe = inspect_application_state
        fetch = api.fetch_dokploy_target_payload

        def old_host_removed(**kwargs: Any) -> Any:
            if kwargs["application"] == "old-app":
                raise ValueError("The retired ancestor's server no longer exists")
            return observe(**kwargs)

        def old_application_removed(**kwargs: Any) -> Any:
            self.assertNotEqual(kwargs["target_id"], self.flow.old_id)
            return fetch(**kwargs)

        with (
            patch.object(rebuild, "inspect_application_state", side_effect=old_host_removed),
            patch.object(api, "fetch_dokploy_target_payload", side_effect=old_application_removed),
        ):
            result = self.execute(3)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertIsNone(self.hold())

    def test_retained_ancestor_preview_does_not_block_rebuilding_testing(self) -> None:
        serving = self.retain_original()
        self.flow.payloads[self.flow.old_id]["previewDeployments"] = [{"id": "preview"}]
        result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [serving])

    def test_retained_ancestor_schedule_does_not_block_rebuilding_testing(self) -> None:
        serving = self.retain_original()
        request = api.dokploy_request

        def schedule(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/schedule.list" and kwargs["query"]["id"] == self.flow.old_id:
                return [{"scheduleId": "cron", "applicationId": self.flow.old_id, "enabled": True}]
            return request(**kwargs)

        with patch.object(api, "dokploy_request", side_effect=schedule):
            result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [serving])

    def assert_late_prerequisite_is_reported(self, *, preview: bool) -> None:
        previous = self.retain_original()
        late = False
        request = api.dokploy_request

        def changed(phase: str) -> None:
            nonlocal late
            if phase == "testing_rebuild_finalization":
                late = True
                if preview:
                    self.flow.payloads[self.flow.old_id]["previewDeployments"] = [{"id": "preview"}]

        def schedule(**kwargs: Any) -> Any:
            if (
                late
                and not preview
                and kwargs["path"] == "/api/schedule.list"
                and kwargs["query"]["id"] == self.flow.old_id
            ):
                return [{"scheduleId": "cron", "applicationId": self.flow.old_id, "enabled": True}]
            return request(**kwargs)

        self.authorize.side_effect = changed
        with patch.object(api, "dokploy_request", side_effect=schedule):
            result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertEqual(self.deleted, [previous])
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.normal("after-retained-ancestor-change").status, "completed")
        self.authorize.side_effect = None
        self.flow.payloads[self.flow.old_id]["previewDeployments"] = []
        self.assertEqual(self.execute(3).post_deploy_status, "pass")
        self.assertNotIn(self.flow.old_id, self.flow.payloads)

    def test_late_ancestor_preview_does_not_hold_the_healthy_testing_lane(self) -> None:
        self.assert_late_prerequisite_is_reported(preview=True)

    def test_late_ancestor_schedule_does_not_hold_the_healthy_testing_lane(self) -> None:
        self.assert_late_prerequisite_is_reported(preview=False)

    def test_lost_clear_reply_replays_the_recorded_retained_ancestor(self) -> None:
        previous = self.retain_original()
        apply = apply_testing_hold_plan

        def changed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                self.flow.payloads[self.flow.old_id]["previewDeployments"] = [{"id": "preview"}]

        def lost(**kwargs: Any) -> Any:
            result = apply(**kwargs)
            if not kwargs["request"].hold:
                raise OSError("Lost clear reply after recording the retained predecessor")
            return result

        self.authorize.side_effect = changed
        with patch.object(rebuild, "apply_testing_hold_plan", side_effect=lost):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(2)
        self.assertIsNone(self.hold())
        self.assertEqual(self.flow.normal("after-retained-clear-reply-loss").status, "completed")
        self.authorize.side_effect = None
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertEqual(self.deleted, [previous])

    def test_enduring_unverified_copy_does_not_revisit_retired_intermediate_hosts(self) -> None:
        def removed(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                self.flow.payloads.pop(self.flow.old_id, None)

        self.authorize.side_effect = removed
        first = self.execute(1)
        second = self.execute(2)
        self.assertEqual(second.post_deploy_status, "fail")
        observe = inspect_application_state

        def obsolete_host(**kwargs: Any) -> Any:
            if kwargs["application"] == first.target_name:
                raise ValueError("The retired intermediate host no longer exists")
            return observe(**kwargs)

        with patch.object(rebuild, "inspect_application_state", side_effect=obsolete_host):
            third = self.execute(3)
        self.assertEqual(third.health_status, "pass")
        self.assertEqual(third.post_deploy_status, "fail")
        self.assertEqual(third.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [first.target_id, second.target_id])
        self.assertIn("old-app", self.flow.services)

    def test_newer_unresolved_copy_does_not_revive_an_older_completed_retirement(self) -> None:
        previous = self.retain_original()

        def transferred(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                current = self.store.read_provider_target_record(
                    context_name=self.flow.context, instance_name="testing"
                )
                self.store.write_provider_target_record(
                    current.model_copy(update={"context": "another-lane", "target_id": previous})
                )

        self.authorize.side_effect = transferred
        second = self.execute(2)
        self.assertEqual(second.runtime_source["retained_predecessors"], previous)
        self.assertEqual(self.deleted, [self.flow.old_id])
        observe = inspect_application_state
        obsolete_observations: list[str] = []

        def obsolete_host(**kwargs: Any) -> Any:
            if kwargs["application"] == "old-app":
                obsolete_observations.append(kwargs["application"])
                raise ValueError("The retired older host no longer exists")
            return observe(**kwargs)

        with patch.object(rebuild, "inspect_application_state", side_effect=obsolete_host):
            third = self.execute(3)
        self.assertEqual(obsolete_observations, [])
        self.assertEqual(third.runtime_source["retained_predecessors"], previous)

    def assert_published_predecessor_alias_is_retained(self, *, provider_only: bool) -> None:
        def transferred(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                if provider_only:
                    current = self.store.read_provider_target_record(
                        context_name=self.flow.context, instance_name="testing"
                    )
                    self.store.write_provider_target_record(
                        current.model_copy(
                            update={"context": "another-lane", "target_id": self.flow.old_id}
                        )
                    )
                else:
                    target = self.store.read_dokploy_target_id_record(
                        context_name=self.flow.context, instance_name="testing"
                    )
                    self.store.write_dokploy_target_id_record(
                        target.model_copy(
                            update={"context": "another-lane", "target_id": self.flow.old_id}
                        )
                    )

        self.authorize.side_effect = transferred
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertIsNone(self.hold())
        self.assertEqual(self.deleted, [])
        self.assertIn(self.flow.old_id, self.flow.payloads)
        self.assertEqual(self.flow.normal("after-old-target-transfer").status, "completed")

    def test_published_immediate_predecessor_provider_alias_reports_not_retired(self) -> None:
        self.assert_published_predecessor_alias_is_retained(provider_only=True)

    def test_published_immediate_predecessor_target_alias_reports_not_retired(self) -> None:
        self.assert_published_predecessor_alias_is_retained(provider_only=False)

    def test_published_current_recipient_alias_stays_protected(self) -> None:
        def shared(phase: str) -> None:
            if phase == "testing_rebuild_finalization":
                current = self.store.read_provider_target_record(
                    context_name=self.flow.context, instance_name="testing"
                )
                self.store.write_provider_target_record(
                    current.model_copy(update={"context": "another-lane"})
                )

        self.authorize.side_effect = shared
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(1)
        self.assertIsNotNone(self.hold())
        self.assertEqual(self.deleted, [])

    def test_missing_unresolved_plan_evidence_keeps_the_next_rebuild_fenced(self) -> None:
        self.retain_original()
        read = self.store.read_idempotency_record
        projects = len(self.flow.projects)

        def incomplete(**kwargs: Any) -> Any:
            row = read(**kwargs)
            if row is not None and row.idempotency_key == "generic-cycle-1:finalization":
                response = deepcopy(row.response_payload)
                response["result"]["runtime_source"].pop("retained_predecessor_plans")
                return row.model_copy(update={"response_payload": response})
            return row

        with patch.object(self.store, "read_idempotency_record", side_effect=incomplete):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(2)
        self.assertIsNone(self.hold())
        self.assertEqual(len(self.flow.projects), projects)
        self.assertEqual(self.deleted, [])
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            self.assertEqual(self.execute(2).post_deploy_status, "pass")
