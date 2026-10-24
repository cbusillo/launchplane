from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOdooLaneDataPolicy,
)
from control_plane.disposable_lane_collection import collect_testing_resource_facts
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_restore import read_current_testing_generation
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_odoo_testing_restore import generation


class HeldTestingResourceCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Mock(spec=PostgresRecordStore)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.profile = self.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                data_authority="resettable"
                            )
                        }
                    )
                    for lane in self.profile.lanes
                )
            }
        )
        self.generation = generation().model_copy(
            update={
                "context": "cm",
                "completed_at": "2026-10-15T00:02:00Z",
                "reconcile_requested_at": "2026-10-15T00:01:00Z",
            }
        )
        resources = OdooTestingResources(
            product=self.generation.product,
            context=self.generation.context,
            operation_id=self.generation.operation_id,
            target_id=self.generation.target_id,
            namespace=self.generation.namespace,
        )
        self.target = DokployTargetRecord(
            context=self.generation.context,
            instance="testing",
            env={
                **resources.volumes,
                "ODOO_PROJECT_NAME": resources.namespace,
                "ODOO_STACK_NAME": resources.namespace,
            },
            updated_at=self.profile.updated_at,
        )
        self.target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Qualify retained resources", purpose="isolation_staging"
        )
        self.store.read_product_profile_record.return_value = self.profile
        self.store.list_product_profile_records.return_value = (self.profile,)
        self.store.read_dokploy_target_record.return_value = self.target
        self.store.read_dokploy_target_id_record.return_value = DokployTargetIdRecord(
            context=self.generation.context,
            instance="testing",
            target_id=self.generation.target_id,
            updated_at=self.profile.updated_at,
        )
        self.store.find_testing_candidate_by_target.return_value = None
        self.store.read_route_binding_record.side_effect = FileNotFoundError
        self.store.read_fresh_odoo_testing_generation.return_value = self.generation
        self.store.read_disposable_resource_preparation.return_value = SimpleNamespace(
            replacement_target_id=resources.target_id,
            resource_namespace=resources.namespace,
            resources_binding_sha256=self.generation.resources_binding_sha256,
        )
        self.store.list_runtime_environment_records.return_value = ()
        self.store.list_dokploy_target_id_records.return_value = ()
        self.store.list_physical_provider_target_records.return_value = ()
        self.credentials = self.enterContext(
            patch("control_plane.odoo_testing_restore.require_distinct_testing_credentials")
        )
        self.inspection = self.enterContext(
            patch(
                "control_plane.odoo_testing_drill_usage.read_drill_recent_signins",
                return_value=None,
            )
        )

    def collect(self) -> dict[str, str] | None:
        return collect_testing_resource_facts(
            self.store,
            product=self.generation.product,
            context=self.generation.context,
            now=datetime.now(timezone.utc),
        )

    def test_read_only_collection_qualifies_resources_under_unchanged_isolation_hold(self) -> None:
        before = self.target.model_dump(mode="json")
        facts = self.collect()
        assert facts is not None
        self.assertEqual(facts.get("data"), "reproducible")
        self.assertEqual(facts.get("production_resources"), "isolated")
        self.assertTrue(self.inspection.called)
        self.assertEqual(self.target.model_dump(mode="json"), before)
        with self.assertRaises(ValueError):
            read_current_testing_generation(
                self.store,
                product=self.generation.product,
                context=self.generation.context,
                instance="testing",
            )

    def test_read_only_prerequisites_expose_copy_blocker_under_isolation_hold(self) -> None:
        self._assert_held_prerequisite_read()

    def test_published_prerequisites_keep_diagnostic_and_action_hold_boundaries(self) -> None:
        with patch(
            "control_plane.testing_serving_copy.published_testing_copy", return_value=Mock()
        ):
            self._assert_held_prerequisite_read()

    def _assert_held_prerequisite_read(self) -> None:
        from control_plane.copy_readback_diagnostics import ReadbackPredicate
        from control_plane.dokploy.native_copy_stream import NativeProgramFailed
        from control_plane.odoo_walkthrough import (
            WalkthroughPrerequisite,
            WalkthroughPrerequisiteError,
            plan_walkthrough,
            read_walkthrough_prerequisites,
        )
        from control_plane.contracts.odoo_walkthrough import OdooWalkthroughRequest
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.odoo_runtime_reads import OdooRuntimeSelection

        before = self.target.model_dump(mode="json")
        with patch(
            "control_plane.odoo_walkthrough.read_drill_recent_signins",
            side_effect=NativeProgramFailed(
                "private receiver detail",
                predicate=ReadbackPredicate.RUNNER_RUNNING,
                kind="refused",
            ),
        ) as inspection:
            checks = {
                checkpoint: (code, state)
                for checkpoint, code, state in read_walkthrough_prerequisites(
                    self.store,
                    Path("/fixture"),
                    self.generation.product,
                    self.generation.context,
                )
            }
        self.assertEqual(checks[WalkthroughPrerequisite.GENERATION][1], "clear")
        self.assertEqual(
            checks[WalkthroughPrerequisite.PHYSICAL_COPY],
            (ReadbackPredicate.RUNNER_RUNNING, "blocked"),
        )
        self.assertEqual(checks[WalkthroughPrerequisite.USE][1], "blocked")
        inspection.assert_called_once()
        self.assertEqual(self.target.model_dump(mode="json"), before)
        with self.assertRaises(ValueError):
            read_current_testing_generation(
                self.store,
                product=self.generation.product,
                context=self.generation.context,
                instance="testing",
            )
        selection = OdooRuntimeSelection(
            RuntimeIdentity(
                product=self.generation.product,
                context=self.generation.context,
                instance="testing",
                deployment_record_id="fixture-deployment",
                artifact_id="fixture-artifact",
                source_git_ref="a" * 40,
            ),
            "https://" + self.generation.domain_host,
        )
        with (
            patch("control_plane.odoo_walkthrough.select_stable_runtime", return_value=selection),
            self.assertRaises(WalkthroughPrerequisiteError) as refused,
        ):
            plan_walkthrough(
                self.store,
                Path("/fixture"),
                OdooWalkthroughRequest(
                    product=self.generation.product,
                    context=self.generation.context,
                    task_id="a" * 32,
                    operation="provision",
                ),
                "fixture-actor",
            )
        self.assertEqual(refused.exception.code, WalkthroughPrerequisite.GENERATION)
        hold = self.target.policies.staff_testing_hold
        assert hold is not None
        hold.purpose = "staff_testing"
        with patch("control_plane.odoo_walkthrough.read_drill_recent_signins") as inspection:
            staff_checks = {
                checkpoint: state
                for checkpoint, code, state in read_walkthrough_prerequisites(
                    self.store,
                    Path("/fixture"),
                    self.generation.product,
                    self.generation.context,
                )
            }
        self.assertEqual(staff_checks[WalkthroughPrerequisite.GENERATION], "blocked")
        inspection.assert_not_called()

    def test_staff_hold_blocks_collection_and_qualification_reader(self) -> None:
        hold = self.target.policies.staff_testing_hold
        assert hold is not None
        hold.purpose = "staff_testing"
        self.assertFalse(self.collect())
        self.inspection.assert_not_called()
        with self.assertRaises(ValueError):
            read_current_testing_generation(
                self.store,
                product=self.generation.product,
                context=self.generation.context,
                instance="testing",
                resource_qualification=True,
            )

    def test_published_reads_and_cleanup_do_not_require_managed_production_credentials(
        self,
    ) -> None:
        from control_plane.odoo_testing_restore import TestingCredentialVersionsMissing

        self.store.read_product_profile_record.return_value = self.profile.model_copy(
            update={
                "lanes": tuple(lane for lane in self.profile.lanes if lane.instance == "testing")
            }
        )
        self.target.policies.staff_testing_hold = None
        self.credentials.side_effect = TestingCredentialVersionsMissing(
            "fixture has no managed production"
        )
        with patch(
            "control_plane.testing_serving_copy.published_testing_copy", return_value=Mock()
        ):
            for generation in (None, self.generation):
                self.store.read_fresh_odoo_testing_generation.return_value = generation
                for qualification in (False, True):
                    with self.subTest(generation=generation, qualification=qualification):
                        self.assertEqual(
                            read_current_testing_generation(
                                self.store,
                                product=self.generation.product,
                                context=self.generation.context,
                                instance="testing",
                                resource_qualification=qualification,
                            ),
                            generation,
                        )
            self.target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
                reason="Staff using the copy", purpose="staff_testing"
            )
            self.assertEqual(
                read_current_testing_generation(
                    self.store,
                    product=self.generation.product,
                    context=self.generation.context,
                    instance="testing",
                    task_account_cleanup=True,
                ),
                self.generation,
            )
            self.credentials.assert_not_called()
            self.target.policies.staff_testing_hold.purpose = "isolation_staging"
            self.assertEqual(self.collect(), {})
            self.credentials.assert_called_once()
            self.inspection.assert_not_called()

    def test_published_copy_keeps_read_only_isolation_hold_and_staff_protection(self) -> None:
        with patch(
            "control_plane.testing_serving_copy.published_testing_copy", return_value=Mock()
        ):
            self.assertEqual(
                read_current_testing_generation(
                    self.store,
                    product=self.generation.product,
                    context=self.generation.context,
                    instance="testing",
                    resource_qualification=True,
                ),
                self.generation,
            )
            self.credentials.assert_not_called()
            with self.assertRaises(ValueError):
                read_current_testing_generation(
                    self.store,
                    product=self.generation.product,
                    context=self.generation.context,
                    instance="testing",
                )
            hold = self.target.policies.staff_testing_hold
            assert hold is not None
            hold.purpose = "staff_testing"
            with self.assertRaises(ValueError):
                read_current_testing_generation(
                    self.store,
                    product=self.generation.product,
                    context=self.generation.context,
                    instance="testing",
                    resource_qualification=True,
                )

    def test_qualification_keeps_credentials_allocation_and_queue_guards(self) -> None:
        faults: tuple[tuple[Mock, str, object], ...] = (
            (self.credentials, "side_effect", ValueError("overlapping credentials")),
            (self.store.read_disposable_resource_preparation, "return_value", None),
            (
                self.store.read_fresh_odoo_testing_generation,
                "return_value",
                self.generation.model_copy(update={"reconcile_requested_at": ""}),
            ),
            (
                self.store.read_fresh_odoo_testing_generation,
                "return_value",
                self.generation.model_copy(update={"completed_at": ""}),
            ),
        )
        for dependency, field, failure in faults:
            with self.subTest(dependency=dependency, field=field):
                previous = getattr(dependency, field)
                setattr(dependency, field, failure)
                try:
                    self.assertFalse(self.collect())
                    self.inspection.assert_not_called()
                finally:
                    setattr(dependency, field, previous)

    def test_changed_resource_authority_still_refuses_before_physical_inspection(self) -> None:
        self.target.env["ODOO_PROJECT_NAME"] = "another-copy"
        self.assertEqual(self.collect(), {})
        self.target.env["ODOO_PROJECT_NAME"] = self.generation.namespace
        self.store.list_dokploy_target_id_records.return_value = (
            DokployTargetIdRecord(
                context="other",
                instance="prod",
                target_id=self.generation.target_id,
                updated_at=self.profile.updated_at,
            ),
        )
        self.assertEqual(self.collect(), {})
        self.inspection.assert_not_called()

    def test_failed_physical_inspection_never_becomes_an_isolation_fact(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        self.inspection.side_effect = ValueError("physical resources unavailable")
        self.assertEqual(self.collect(), {})
        self.inspection.side_effect = NativeSourceAuthorizationDenied("provider refused")
        with self.assertRaises(NativeSourceAuthorizationDenied):
            self.collect()

    def test_production_is_never_selected_for_qualification(self) -> None:
        self.assertIsNone(
            read_current_testing_generation(
                self.store,
                product=self.generation.product,
                context=self.generation.context,
                instance="prod",
                resource_qualification=True,
            )
        )
        self.store.read_fresh_odoo_testing_generation.assert_not_called()
