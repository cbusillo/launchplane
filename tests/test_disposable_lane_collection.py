from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
    TestingHoldPurpose,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOdooLaneDataPolicy,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.disposable_lane_collection import (
    collect_testing_resource_facts,
    collect_disposable_lane_facts,
)
from control_plane.disposable_lane_collection import merge_collected_safety
from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_restore import read_current_testing_generation
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_odoo_testing_restore import generation
from tests.test_disposable_lane_authority import _DisposableRuntimeAuthorityFixture


class UsageEvidenceExpiryTests(unittest.TestCase):
    def test_legacy_hold_with_unusable_timestamp_remains_protective(self) -> None:
        store = Mock(spec=PostgresRecordStore)
        store.list_dokploy_target_id_records.return_value = ()
        for stamp in ("", "old-format", "2026-11-07T18:00:00"):
            target = DokployTargetRecord(
                context="fixture", instance="testing", updated_at="2026-11-07T00:00:00Z"
            )
            target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
                reason="Actual staff use",
                purpose="staff_testing",
                recorded_by="fixture-staff",
                recorded_at=stamp,
            )
            with self.subTest(stamp=stamp):
                facts = collect_disposable_lane_facts(
                    store,
                    context="fixture",
                    instance="testing",
                    target=target,
                    target_id=None,
                    binding_sha256="a" * 64,
                )
                self.assertEqual(facts.usage, "in_use")
                self.assertEqual(facts.usage_actor, target.policies.staff_testing_hold.recorded_by)
                self.assertIsNone(facts.usage_since)
                self.assertIsNone(facts.usage_valid_until)

    def test_new_use_keeps_its_deadline_when_resource_proof_expires_first(self) -> None:
        now = datetime.now(timezone.utc)
        previous = DisposableLaneSafety(
            product="fixture",
            context="fixture",
            instance="testing",
            observed_by="collector",
            observer_kind="director_agent",
            binding_sha256="a" * 64,
            data="reproducible",
            data_evidence="fixture:rebuildable",
            production_resources="isolated",
            isolation_evidence="fixture:isolated",
            observed_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        collected = previous.model_copy(
            update={
                "observed_at": now + timedelta(seconds=10),
                "expires_at": now + timedelta(minutes=5, seconds=10),
                "usage": "in_use",
                "usage_evidence": "fixture:session",
                "usage_since": now + timedelta(seconds=11),
                "usage_valid_until": now + timedelta(minutes=5, seconds=10),
                "data": "unknown",
                "production_resources": "unknown",
            }
        )
        merged = merge_collected_safety(previous, collected)
        self.assertEqual(merged.expires_at, previous.expires_at)
        self.assertEqual(merged.usage_valid_until, collected.usage_valid_until)
        self.assertEqual(merged.current_usage(previous.expires_at), "in_use")

    def test_recollection_preserves_use_deadline_and_independent_protection(self) -> None:
        now = datetime.now(timezone.utc)
        previous = DisposableLaneSafety(
            product="fixture",
            context="fixture",
            instance="testing",
            observed_by="collector",
            observer_kind="director_agent",
            binding_sha256="a" * 64,
            usage="in_use",
            usage_actor="odoo-user:42",
            usage_since=now - timedelta(minutes=1),
            usage_valid_until=now + timedelta(minutes=1),
            usage_evidence="fixture:session",
            data="only_copy",
            data_evidence="fixture:needed-copy",
            production_resources="shared",
            isolation_evidence="fixture:shared-resources",
            observed_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        for elapsed in (30, 61):
            collected = previous.model_copy(
                update={
                    "usage": "unknown",
                    "usage_evidence": "",
                    "usage_actor": "",
                    "usage_since": None,
                    "usage_valid_until": None,
                    "data": "unknown",
                    "production_resources": "unknown",
                    "observed_at": now + timedelta(seconds=elapsed),
                    "expires_at": now + timedelta(minutes=6),
                }
            )
            merged = merge_collected_safety(previous, collected)
            self.assertEqual(merged.usage, "in_use" if elapsed == 30 else "unknown")
            if elapsed == 30:
                self.assertEqual(merged.usage_valid_until, previous.usage_valid_until)
                self.assertEqual(merged.usage_actor, previous.usage_actor)
            self.assertEqual(
                (merged.data, merged.production_resources),
                (previous.data, previous.production_resources),
            )


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
            updated_at=self.profile.updated_at,
        )
        self.runtime = RuntimeEnvironmentRecord(
            scope="instance",
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
        self.store.list_runtime_environment_records.return_value = (self.runtime,)
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
                    facts = self.collect()
                    if dependency is self.credentials:
                        self.assertEqual(facts, {})
                        with patch.object(
                            self.store, "read_fresh_odoo_testing_generation", return_value=None
                        ):
                            self.assertEqual(self.collect(), {})
                    else:
                        self.assertFalse(facts)
                    self.inspection.assert_not_called()
                finally:
                    setattr(dependency, field, previous)

    def test_changed_resource_authority_still_refuses_before_physical_inspection(self) -> None:
        self.runtime.env["ODOO_PROJECT_NAME"] = "another-copy"
        self.assertEqual(self.collect(), {})
        self.runtime.env["ODOO_PROJECT_NAME"] = self.generation.namespace
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


class CurrentGenerationUsageAgreementTests(_DisposableRuntimeAuthorityFixture):
    def test_initial_generation_still_protects_current_use_without_creation_history(self) -> None:
        from control_plane import odoo_testing_usage
        from control_plane.dokploy import api, source
        from control_plane.testing_initial_generation import require_testing_generation_usage

        profile = self.store.read_product_profile_record("example-site").model_copy(
            update={"driver_id": "odoo"}
        )
        self.store.write_product_profile_record(profile)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        identity = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        with (
            patch.object(
                source, "read_dokploy_config", return_value=("https://provider.invalid", "fixture")
            ),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "name": target.target_name,
                    "appName": "fixture",
                    "serverId": "fixture",
                },
            ),
            patch.object(
                odoo_testing_usage,
                "_read_remote",
                side_effect=lambda **kw: kw["receive"](
                    {"type": "complete", "recent_signins": True, "authenticated_sessions": False}
                ),
            ) as probe,
            self.assertRaises(ValueError),
        ):
            require_testing_generation_usage(
                Path(self.temporary.name),
                self.store,
                profile.product,
                self.context,
                target,
                identity,
            )
        probe.assert_called()

    async def test_isolation_hold_keeps_collection_inspection_and_admission_on_one_usage_answer(
        self,
    ) -> None:
        from control_plane import odoo_testing_usage, testing_rebuild_service
        from control_plane.dokploy import api, source
        from control_plane.testing_rebuild_driver import TestingRebuildRefused

        profile = self.store.read_product_profile_record("example-site").model_copy(
            update={"driver_id": "odoo"}
        )
        self.store.write_product_profile_record(profile)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        identity = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        current_generation = generation().model_copy(
            update={
                "product": profile.product,
                "context": self.context,
                "target_id": identity.target_id,
                "prepared_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                "completed_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            }
        )
        self.store.write_fresh_odoo_testing_generation(
            current_generation.model_copy(update={"completed_at": ""})
        )
        self.store.write_fresh_odoo_testing_generation(current_generation)
        target.target_name = current_generation.namespace
        operation: Any = SimpleNamespace(
            product=profile.product,
            context=self.context,
            authorization=None,
            request=SimpleNamespace(data_source_mode="empty"),
        )
        scenarios: tuple[tuple[str, TestingHoldPurpose, str], ...] = (
            ("unused", "isolation_staging", ""),
            ("in_use", "isolation_staging", "testing_in_use"),
            ("unknown", "isolation_staging", ""),
            ("unused", "staff_testing", "testing_staff_hold"),
        )
        for usage, purpose, refusal in scenarios:
            with self.subTest(usage=usage, purpose=purpose):
                self.store.clear_disposable_lane_safety(profile.product, self.context, "testing")
                target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
                    purpose=purpose,
                    reason="Fixture use",
                    recorded_by="fixture-agent",
                    recorded_at=target.updated_at,
                )
                self.store.write_dokploy_target_record(target)
                intent: Any = SimpleNamespace(
                    profile=profile,
                    before=SimpleNamespace(target=target, target_id=identity),
                    old=None,
                )
                frame = {
                    "type": "complete",
                    "recent_signins": usage == "in_use",
                    "authenticated_sessions": False,
                }
                if usage == "unknown":
                    frame = {
                        "type": "complete",
                        "unavailable": True,
                        "reason_code": "database_connection_unavailable",
                    }
                with (
                    patch.object(
                        source,
                        "read_dokploy_config",
                        return_value=("https://provider.invalid", "fixture"),
                    ),
                    patch.object(
                        api,
                        "fetch_dokploy_target_payload",
                        return_value={
                            "name": target.target_name,
                            "appName": current_generation.namespace + "-abc123",
                            "serverId": "fixture",
                        },
                    ),
                    patch.object(
                        odoo_testing_usage,
                        "_read_remote",
                        side_effect=lambda **kw: kw["receive"](frame),
                    ) as probe,
                    patch(
                        "control_plane.http_app.collect_testing_resource_facts", return_value=None
                    ),
                ):
                    before = await self.observe()
                    collected = await self.observe(
                        mode="collect",
                        expected_binding_sha256=before.json()["result"]["binding_sha256"],
                    )
                    self.assertEqual(collected.status_code, 202, collected.text)
                    probe.assert_called()
                    expected = "in_use" if purpose == "staff_testing" else usage
                    self.assertEqual(collected.json()["result"]["usage"], expected)
                    self.assertEqual(
                        collected.json()["result"]["odoo_usage"]["reason_code"],
                        frame.get("reason_code", "complete"),
                    )
                    inspected = await self.observe()
                    self.assertEqual(inspected.json()["result"]["observation"]["usage"], expected)
                    self.store.clear_disposable_lane_safety(
                        profile.product, self.context, "testing"
                    )
                    probe.reset_mock()
                    if refusal:
                        with self.assertRaises(TestingRebuildRefused) as caught:
                            testing_rebuild_service.check_testing_rebuild_protection(
                                Path(self.temporary.name),
                                self.store,
                                operation,
                                intent,
                                actor="fixture-agent",
                            )
                        self.assertEqual(caught.exception.code, refusal)
                    else:
                        testing_rebuild_service.check_testing_rebuild_protection(
                            Path(self.temporary.name),
                            self.store,
                            operation,
                            intent,
                            actor="fixture-agent",
                        )
                    if purpose == "isolation_staging":
                        probe.assert_called()
                self.assertEqual(
                    self.store.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    ),
                    target,
                )
