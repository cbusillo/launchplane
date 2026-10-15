"""Behavior still consumed by historical-policy validation and native maintenance."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentBudget,
    OrdinaryAgentPolicyRule,
    OrdinaryAgentPullRequest,
    OrdinaryAgentTarget,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.ordinary_agent_delivery_authorization_inputs import (
    MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS,
    _read_current_inventory,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_repository_inventory import _record


class RetainedInventoryProjectionTests(unittest.TestCase):
    def test_latest_tie_is_ambiguous_and_retired_latest_never_falls_back(self) -> None:
        older = _record()
        newer = _record(revision=2, state="retired", supersedes_record_id=older.record_id)
        conflicting = RepositoryInventoryRecord.model_validate(
            {**older.model_dump(exclude={"inventory_digest"}), "reason": "conflicting history"}
        )
        for records, expected_state in (
            ((older, newer), "complete"),
            ((older, conflicting), "ambiguous"),
        ):
            with self.subTest(state=expected_state):
                store = SimpleNamespace(list_repository_inventory_records=lambda **kwargs: records)
                state, selected, _ = _read_current_inventory(store)
                self.assertEqual(state, expected_state)
                self.assertEqual(selected, ())

    def test_bounded_and_unavailable_reads_never_return_partial_or_private_data(self) -> None:
        private_detail = "synthetic-private-inventory-error"
        unavailable = SimpleNamespace(
            list_repository_inventory_records=Mock(side_effect=RuntimeError(private_detail))
        )
        oversized = SimpleNamespace(
            list_repository_inventory_records=lambda **kwargs: (
                (_record(),) * (MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS + 1)
            )
        )
        for store, expected_state in ((unavailable, "unavailable"), (oversized, "truncated")):
            with self.subTest(state=expected_state):
                state, selected, diagnostics = _read_current_inventory(store)
                self.assertEqual(state, expected_state)
                self.assertEqual(selected, ())
                self.assertNotIn(private_detail, repr(diagnostics))


class RetainedPolicyStorageBoundTests(unittest.TestCase):
    def test_policy_limit_bounds_database_payload_decoding(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'policies.sqlite3'}"
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            for index in range(3):
                store.write_merge_train_policy_record(
                    build_test_merge_train_policy_record(
                        record_id=f"superseded-policy-{index}",
                        updated_at=f"2026-09-12T12:0{index}:00Z",
                    ).model_copy(update={"status": "superseded"})
                )
            for limit in (1, 2):
                with self.subTest(limit=limit):
                    with patch.object(store, "_read_payload", wraps=store._read_payload) as read:
                        records = store.list_merge_train_policy_records(
                            status="superseded", limit=limit
                        )
                    self.assertEqual(len(records), limit)
                    self.assertEqual(read.call_count, limit)


class RetainedHistoricalContractValidationTests(unittest.TestCase):
    def test_stored_targets_remain_exact_and_strict(self) -> None:
        target = {"repository_id": 123, "repository": "example/repo", "base_branch": "main"}
        for changes in (
            {"repository": "example/*"},
            {"repository": " example/repo"},
            {"repository_id": "123"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                OrdinaryAgentTarget.model_validate({**target, **changes})
        with self.assertRaises(ValidationError):
            OrdinaryAgentPolicyRule.model_validate(
                {
                    "managed_set_id": "invalid managed id",
                    "managed_rule_id": "legacy-rule",
                    "principal_id": "legacy-agent",
                    "target": target,
                    "actions": ["self_read"],
                }
            )

    def test_stored_heads_and_budgets_keep_their_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            OrdinaryAgentPullRequest(number=7, head_sha="AB" * 20)
        with self.assertRaises(ValidationError):
            OrdinaryAgentBudget(
                window_start=0,
                window_end=1,
                action_limit=-1,
                actions_used=0,
                pull_request_limit=1,
                pull_requests_used=0,
            )
