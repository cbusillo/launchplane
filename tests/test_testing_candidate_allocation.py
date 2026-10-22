"""A candidate has independent custody before any serving authority changes."""

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.dokploy import api, source, sandbox_placement
from control_plane.dokploy import native_copy_stream, network_capacity
from control_plane.dokploy.network_capacity import (
    BRIDGE_NETWORK_CAPACITY_MARGIN,
    BridgeNetworkCapacity,
    BridgeNetworkCapacityRefused,
)
from control_plane.provider_operations import ProviderMutationRejectedError, ProviderOperationLease
from control_plane.workflows.provider_target_audit import audit_provider_targets
from control_plane.workflows.provider_target_backfill import backfill_provider_targets
from control_plane import testing_candidate_allocation as candidate
from control_plane.workflows.dokploy_target_adoption import (
    PROJECT_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    COMPOSE_CREATE_PATH,
)
from tests import test_testing_replacement_resources as resources


class TestingCandidateTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        fixture = resources.TestingReplacementResourceFixture(self, database_url=self.database_url)
        self.store, self.root = fixture.store, fixture.root
        self.context, self.product = fixture.context, fixture.profile.product
        self.allocation = TestingCandidateAllocation(
            namespace=testing_candidate_namespace(self.product, self.context, "rebuild-original"),
            product=self.product,
            context=self.context,
            operation_id="rebuild-original",
            intent_sha256=canonical_json_sha256({"source": "empty", "artifact": "new-build"}),
            serving_target_id="compose-testing",
            server_id="testing-server",
            created_at=datetime.now(timezone.utc),
        )
        self.before = self.authority()
        self.guard = Mock(side_effect=self.assert_serving_unchanged)
        self.lease = Mock(spec=ProviderOperationLease)
        self.placement: tuple[str, str, str] = ("", "", "")
        self.requests: list[str] = []
        self.free_networks = 1 + BRIDGE_NETWORK_CAPACITY_MARGIN
        self.capacity_read = self.enterContext(
            patch.object(
                network_capacity, "read_bridge_network_capacity", side_effect=self.capacity
            )
        )
        self.enterContext(
            patch.object(
                source, "read_dokploy_config", return_value=("provider.invalid", "fixture")
            )
        )
        self.enterContext(
            patch.object(
                sandbox_placement, "created_placement", side_effect=lambda *_: self.placement
            )
        )
        self.enterContext(
            patch.object(candidate, "created_placement", side_effect=lambda *_: self.placement)
        )
        self.request = self.enterContext(
            patch.object(api, "dokploy_request", side_effect=self.provider)
        )
        self.storage = self.enterContext(
            patch.object(candidate, "run_resource_schedule", side_effect=self.resource_schedule)
        )

    def capacity(self, **kwargs: object) -> BridgeNetworkCapacity:
        self.assertEqual(kwargs["server_id"], self.allocation.server_id)
        self.assertFalse(self.requests)
        return BridgeNetworkCapacity(total_slots=self.free_networks, free_slots=self.free_networks)

    def test_short_capacity_or_missing_margin_refuses_before_allocation(self) -> None:
        for free in (0, BRIDGE_NETWORK_CAPACITY_MARGIN):
            with self.subTest(free=free):
                self.free_networks = free
                with self.assertRaises(ProviderMutationRejectedError) as refused:
                    self.prepare()
                cause = refused.exception.cause
                assert isinstance(cause, BridgeNetworkCapacityRefused)
                self.assertEqual(cause.code, "host_bridge_network_capacity_short")
                self.assertIsNone(self.store.read_testing_candidate(self.allocation.namespace))
                self.assertFalse(self.requests)
                self.storage.assert_not_called()
                self.lease.checkpoint_effect.assert_not_called()
                self.assert_serving_unchanged()

    def test_exact_margin_admits_and_existing_candidate_skips_capacity(self) -> None:
        self.prepare()
        self.capacity_read.assert_called_once()
        self.capacity_read.side_effect = AssertionError("Recovery must use original custody")
        self.prepare()
        self.assertEqual(self.storage.call_args.kwargs["mode"], "readback")

    def test_unavailable_capacity_and_provider_denial_preserve_their_types(self) -> None:
        denial = native_copy_stream.NativeSourceAuthorizationDenied("fixture denial")
        for result in (
            BridgeNetworkCapacityRefused("host_bridge_network_capacity_unavailable"),
            denial,
        ):
            with self.subTest(result=result):
                self.capacity_read.side_effect = result
                with self.assertRaises(ProviderMutationRejectedError) as refused:
                    self.prepare()
                if result is denial:
                    self.assertIs(refused.exception.cause, denial)
                else:
                    cause = refused.exception.cause
                    assert isinstance(cause, BridgeNetworkCapacityRefused)
                    self.assertEqual(cause.code, "host_bridge_network_capacity_unavailable")
                self.assertIsNone(self.store.read_testing_candidate(self.allocation.namespace))
                self.assertFalse(self.requests)
                self.lease.checkpoint_effect.assert_not_called()

    def test_unrecorded_or_ambiguous_placement_precedes_capacity(self) -> None:
        for observation in (
            ("existing-project", "", ""),
            sandbox_placement.IsolatedPlacementObservationRequired("ambiguous inventory"),
        ):
            with self.subTest(observation=observation):
                with patch.object(candidate, "created_placement") as placement:
                    if isinstance(observation, Exception):
                        placement.side_effect = observation
                    else:
                        placement.return_value = observation
                    with self.assertRaises(ProviderMutationRejectedError) as refused:
                        self.prepare()
                self.assertIsInstance(
                    refused.exception.cause, sandbox_placement.IsolatedPlacementObservationRequired
                )
                self.capacity_read.assert_not_called()
                self.assertIsNone(self.store.read_testing_candidate(self.allocation.namespace))
                self.assertFalse(self.requests)
                self.lease.checkpoint_effect.assert_not_called()

    def authority(self) -> tuple[object, ...]:
        return (
            self.store.read_product_profile_record(self.product),
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ),
            self.store.read_provider_target_record(
                context_name=self.context, instance_name="testing"
            ),
            self.store.read_fresh_odoo_testing_generation(self.product, self.context),
            self.store.list_runtime_environment_records(),
            self.store.list_environment_inventory(),
        )

    def assert_serving_unchanged(self) -> None:
        self.assertEqual(self.authority(), self.before)

    def provider(self, **kwargs: object) -> dict[str, str]:
        self.assert_serving_unchanged()
        self.requests.append(str(kwargs["path"]))
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None
        if kwargs["path"] == PROJECT_CREATE_PATH:
            self.assertEqual(record.effect, "project_id")
            self.placement = ("candidate-project", "", "")
            return {"projectId": self.placement[0]}
        if kwargs["path"] == ENVIRONMENT_CREATE_PATH:
            self.assertEqual(record.project_id, self.placement[0])
            self.assertEqual(record.effect, "environment_id")
            self.placement = (self.placement[0], "candidate-environment", "")
            return {"environmentId": self.placement[1]}
        self.assertEqual(kwargs["path"], COMPOSE_CREATE_PATH)
        self.assertEqual(record.effect, "compose_id")
        self.assertEqual(record.environment_id, self.placement[1])
        self.placement = (*self.placement[:2], "candidate-compose")
        return {"composeId": self.placement[2]}

    def resource_schedule(self, **kwargs: object) -> dict[str, str]:
        callback = kwargs.get("before_provider_mutation")
        if callable(callback):
            callback("schedule_upsert")
        self.assert_serving_unchanged()
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None
        self.assertEqual(record.effect, "storage")
        self.assertEqual(kwargs["resources"], candidate.candidate_resources(record))
        definition = kwargs["candidate_target"]
        assert isinstance(definition, source.DokployTargetDefinition)
        self.assertEqual(definition.target_id, record.compose_id)
        return {}

    def prepare(self) -> TestingCandidateAllocation:
        return candidate.prepare_candidate_storage(
            root=self.root,
            store=self.store,
            allocation=self.allocation,
            lease=self.lease,
            guard=self.guard,
        )

    def test_independent_storage_never_publishes_candidate_as_serving(self) -> None:
        result = self.prepare()
        self.assert_serving_unchanged()
        self.assertEqual(
            result.resources_binding_sha256, candidate.candidate_resources(result).binding_sha256
        )
        self.assertEqual(self.store.read_testing_candidate(result.namespace), result)
        self.assertEqual(
            self.requests, [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, COMPOSE_CREATE_PATH]
        )
        self.assertEqual(self.storage.call_args.kwargs["mode"], "prepare")
        self.prepare()
        self.assertEqual(self.storage.call_args.kwargs["mode"], "readback")
        self.assertEqual(len(self.requests), 3)
        with self.assertRaises(ValueError):
            self.store.write_provider_target_record(
                ProviderTargetRecord(
                    context="other-product",
                    instance="prod",
                    provider_id="dokploy",
                    target_category="compose",
                    provider_target_type="compose",
                    target_id=result.compose_id,
                    display_name="other",
                    updated_at=result.created_at.isoformat(),
                )
            )

    def test_custodied_candidate_is_a_complete_reservation_not_a_missing_serving_pair(self) -> None:
        allocation = self.prepare()
        audit = audit_provider_targets(self.store, context_name=allocation.namespace)
        self.assertTrue(audit.ok)
        self.assertTrue(all(item.severity == "ok" for item in audit.findings))
        before = self.store.list_physical_provider_target_records()
        backfill = backfill_provider_targets(
            self.store, apply=True, context_name=allocation.namespace
        )
        self.assertTrue(backfill.ok)
        self.assertEqual(self.store.list_physical_provider_target_records(), before)
        self.assert_serving_unchanged()

    def test_candidate_reservation_does_not_block_unrelated_backfill_apply(self) -> None:
        from tests.test_provider_target_backfill import (
            _dokploy_target_record,
            _dokploy_target_id_record,
        )

        self.prepare()
        self.store.write_dokploy_target_record(_dokploy_target_record(context="unrelated"))
        self.store.write_dokploy_target_id_record(_dokploy_target_id_record(context="unrelated"))
        result = backfill_provider_targets(self.store, apply=True)
        self.assertTrue(result.ok)
        self.assertIsNotNone(
            self.store.read_provider_target_record(context_name="unrelated", instance_name="prod")
        )
        self.assert_serving_unchanged()

    def test_orphaned_candidate_reservation_keeps_audit_and_backfill_blocked(self) -> None:
        allocation = self.prepare()
        row = self.store.read_provider_target_record(
            context_name=allocation.namespace, instance_name="testing-candidate"
        )
        self.store.write_provider_target_record(row.model_copy(update={"target_id": "orphan"}))
        self.assertFalse(audit_provider_targets(self.store, context_name=allocation.namespace).ok)
        self.assertFalse(
            backfill_provider_targets(self.store, apply=True, context_name=allocation.namespace).ok
        )

    def test_lost_creation_reply_preserves_old_copy_and_never_repeats_effect(self) -> None:
        def lost(**kwargs: object) -> dict[str, str]:
            response = self.provider(**kwargs)
            if kwargs["path"] == ENVIRONMENT_CREATE_PATH:
                raise TimeoutError("lost response")
            return response

        self.request.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.prepare()
        self.assert_serving_unchanged()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(len(self.requests), 2)
        recovered = candidate.observe_candidate_creation(
            root=self.root, store=self.store, namespace=self.allocation.namespace, guard=self.guard
        )
        assert recovered is not None
        self.assertEqual(recovered.environment_id, self.placement[1])
        self.request.side_effect = self.provider
        self.capacity_read.side_effect = AssertionError("Partial placement keeps original custody")
        self.prepare()
        self.assertEqual(self.requests.count(ENVIRONMENT_CREATE_PATH), 1)
        self.assertEqual(self.requests.count(COMPOSE_CREATE_PATH), 1)
        self.assert_serving_unchanged()

    def test_uncertain_absent_creation_stays_unknown(self) -> None:
        self.request.side_effect = TimeoutError("request may still complete")
        with self.assertRaises(TimeoutError):
            self.prepare()
        self.assertIsNone(
            candidate.observe_candidate_creation(
                root=self.root,
                store=self.store,
                namespace=self.allocation.namespace,
                guard=self.guard,
            )
        )
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.request.call_count, 1)
        self.assert_serving_unchanged()

    def test_storage_render_refusal_preserves_previous_effect_checkpoint(self) -> None:
        from control_plane.contracts.provider_schedule import ScheduleMaterializationTooLarge

        def refused(**kwargs: object) -> None:
            raise ScheduleMaterializationTooLarge()

        self.storage.side_effect = refused
        with self.assertRaises(ScheduleMaterializationTooLarge):
            self.prepare()
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None
        self.assertEqual(record.effect, "compose_id")
        self.lease.checkpoint_effect.assert_called_with("compose_id")
        self.lease.checkpoint_effect.reset_mock()
        with self.assertRaises(ScheduleMaterializationTooLarge):
            self.prepare()
        self.lease.checkpoint_effect.assert_not_called()
        self.assertEqual(self.store.read_testing_candidate(record.namespace), record)

    def test_storage_timeout_recovers_by_readback_without_recreating(self) -> None:
        def lost(**kwargs: object) -> dict[str, str]:
            self.resource_schedule(**kwargs)
            raise TimeoutError("schedule reply lost")

        self.storage.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.prepare()
        self.storage.side_effect = self.resource_schedule
        result = self.prepare()
        self.assertEqual(self.storage.call_args.kwargs["mode"], "readback")
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(result.resources_binding_sha256)
        self.assert_serving_unchanged()

    def test_changed_intent_or_stale_checkpoint_cannot_replace_candidate_custody(self) -> None:
        result = self.prepare()
        changed = self.allocation.model_copy(
            update={"intent_sha256": canonical_json_sha256({"source": "upstream"})}
        )
        self.allocation = changed
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            self.store.compare_and_write_testing_candidate(None, result)
        with self.assertRaises(ValueError):
            self.store.compare_and_write_testing_candidate(
                result, result.model_copy(update={"compose_id": "another-target"})
            )
        self.assert_serving_unchanged()

    def test_production_alias_refuses_capture_atomically(self) -> None:
        original = self.provider

        def alias(**kwargs: object) -> dict[str, str]:
            response = original(**kwargs)
            if kwargs["path"] == COMPOSE_CREATE_PATH:
                return {"composeId": "compose-prod"}
            return response

        self.request.side_effect = alias
        with self.assertRaises(ValueError):
            self.prepare()
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None
        self.assertFalse(record.compose_id)
        self.storage.assert_not_called()
        self.assert_serving_unchanged()

    def test_authorization_loss_stops_before_any_new_placement(self) -> None:
        self.guard.side_effect = PermissionError("testing rebuild authority withdrawn")
        with self.assertRaisesRegex(PermissionError, "withdrawn"):
            self.prepare()
        self.request.assert_not_called()
        self.assertIsNone(self.store.read_testing_candidate(self.allocation.namespace))
        self.assert_serving_unchanged()

    def test_legacy_lane_alias_refuses_capture_before_any_candidate_storage(self) -> None:
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="foreign-context",
                instance="prod",
                target_id="candidate-compose",
                updated_at=self.allocation.created_at.isoformat(),
            )
        )
        with self.assertRaises(ValueError):
            self.prepare()
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None
        self.assertFalse(record.compose_id)
        self.storage.assert_not_called()
        self.assertFalse(
            any(
                item.target_id == "candidate-compose"
                for item in self.store.list_physical_provider_target_records()
            )
        )
        self.assert_serving_unchanged()

    def test_alias_added_after_capture_refuses_the_next_provider_checkpoint(self) -> None:
        def prepare_storage(**kwargs: object) -> dict[str, str]:
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="foreign-context",
                    instance="prod",
                    target_id="candidate-compose",
                    updated_at=self.allocation.created_at.isoformat(),
                )
            )
            checkpoint = kwargs["checkpoint"]
            assert callable(checkpoint)
            checkpoint()
            self.fail("An aliased candidate cannot dispatch storage work.")

        self.storage.side_effect = prepare_storage
        with self.assertRaises(ValueError):
            self.prepare()
        record = self.store.read_testing_candidate(self.allocation.namespace)
        assert record is not None
        self.assertFalse(record.resources_binding_sha256)
        self.assert_serving_unchanged()
