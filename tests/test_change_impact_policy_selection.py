import unittest
from unittest.mock import Mock

from control_plane.change_impact_service import (
    evaluate_change_impact,
    get_change_impact_policy_read_model,
)
from tests.test_change_impact import REPOSITORY_ID, _policy, _repository_evidence


class ChangeImpactPolicySelectionTests(unittest.TestCase):
    def test_superseded_history_cannot_authorize_a_fresh_evaluation(self) -> None:
        superseded = _policy().model_copy(update={"status": "superseded"})
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/server.py"),
            policies=(superseded,),
            evaluated_at="2026-08-07T00:00:00Z",
        )
        self.assertEqual(
            (
                result.status,
                result.reason_code,
                result.policy_record_id,
                result.owner_impact,
                result.affected_products,
                result.required_engineering_review_count,
            ),
            ("unknown", "policy_unavailable", "", "unknown", (), 2),
        )
        self.assertIsNone(result.coverage)

    def test_current_read_keeps_history_visible_without_claiming_active_authority(self) -> None:
        superseded = _policy().model_copy(update={"status": "superseded"})
        store = Mock()
        store.list_change_impact_policy_records.return_value = (superseded,)
        result = get_change_impact_policy_read_model(store=store, repository_id=REPOSITORY_ID)
        self.assertEqual(
            (result.repository_id, result.current_policy, result.policy_history_count),
            (REPOSITORY_ID, None, 1),
        )

    def test_active_policy_result_is_identical_with_superseded_history(self) -> None:
        previous = _policy(effective_at="2026-08-01T00:00:00Z")
        current = _policy(revision=2, supersedes_record_id=previous.record_id)
        evidence = _repository_evidence("src/runtime/server.py")
        expected = evaluate_change_impact(
            repository_evidence=evidence,
            policies=(current,),
            evaluated_at="2026-08-07T00:00:00Z",
        )
        actual = evaluate_change_impact(
            repository_evidence=evidence,
            policies=(previous.model_copy(update={"status": "superseded"}), current),
            evaluated_at="2026-08-07T00:00:00Z",
        )
        self.assertEqual(actual, expected)
        self.assertEqual((actual.status, actual.policy_digest), ("success", current.policy_digest))

    def test_timestamp_before_active_policy_does_not_resurrect_superseded_record(self) -> None:
        previous = _policy(effective_at="2026-08-01T00:00:00Z")
        current = _policy(revision=2, supersedes_record_id=previous.record_id)
        for history in (
            (current,),
            (previous.model_copy(update={"status": "superseded"}), current),
        ):
            with self.subTest(history_length=len(history)):
                result = evaluate_change_impact(
                    repository_evidence=_repository_evidence("src/runtime/server.py"),
                    policies=history,
                    evaluated_at="2026-08-05T00:00:00Z",
                )
                self.assertEqual(
                    (result.status, result.reason_code), ("unknown", "policy_unavailable")
                )

    def test_multiple_active_policies_remain_an_invalid_history(self) -> None:
        first = _policy()
        second = _policy(revision=2, supersedes_record_id=first.record_id)
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/server.py"),
            policies=(first, second),
            evaluated_at="2026-08-07T00:00:00Z",
        )
        self.assertEqual((result.status, result.reason_code), ("unknown", "policy_history_invalid"))
