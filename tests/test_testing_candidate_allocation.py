"""A candidate has independent custody before any serving authority changes."""

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.dokploy import api, source, sandbox_placement
from control_plane.provider_operations import ProviderOperationLease
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
        fixture = resources.TestingReplacementResourceHttpTests()
        fixture.database_url = self.database_url
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
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
        with self.assertRaisesRegex(ValueError, "another route"):
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
        with self.assertRaisesRegex(ValueError, "original-key"):
            self.prepare()
        self.assertEqual(len(self.requests), 2)
        recovered = candidate.observe_candidate_creation(
            root=self.root, store=self.store, namespace=self.allocation.namespace, guard=self.guard
        )
        assert recovered is not None
        self.assertEqual(recovered.environment_id, self.placement[1])
        self.request.side_effect = self.provider
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
        with self.assertRaisesRegex(ValueError, "original-key"):
            self.prepare()
        self.assertEqual(self.request.call_count, 1)
        self.assert_serving_unchanged()

    def test_storage_timeout_recovers_by_readback_without_recreating(self) -> None:
        self.storage.side_effect = TimeoutError("schedule reply lost")
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
        with self.assertRaisesRegex(ValueError, "original lane or intent"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "custody changed"):
            self.store.compare_and_write_testing_candidate(None, result)
        with self.assertRaisesRegex(ValueError, "immutable"):
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
        with self.assertRaisesRegex(ValueError, "another route"):
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
