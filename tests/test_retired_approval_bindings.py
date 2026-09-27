"""Legacy identity and inert v2 consumer compatibility across immutable review records."""

import unittest

from pydantic import ValidationError

from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceEventRecord,
    owner_acceptance_event_replay_digest,
)
from tests.test_merge_readiness import _engineering_decision
from tests.test_postgres_integration import _owner_acceptance_event


SEMANTIC_DIGEST = "e" * 64


def _v2_event(*, revision: int = 1) -> OwnerAcceptanceEventRecord:
    payload = _owner_acceptance_event().model_dump(mode="json")
    for key in ("event_id", "acceptance_id"):
        payload.pop(key)
    payload["binding"].pop("binding_sha256")
    payload["binding"].update(
        binding_hash_version=2,
        change_impact_decision_digest=SEMANTIC_DIGEST,
        change_impact_policy_record_id=f"change-impact-policy-1001-r{revision}",
        change_impact_policy_revision=revision,
        change_impact_policy_digest=("a" if revision == 1 else "d") * 64,
    )
    return OwnerAcceptanceEventRecord.model_validate(payload)


def _v2_engineering(*, revision: int = 1) -> EngineeringReviewDecisionRecord:
    payload = _engineering_decision().model_dump(
        mode="json", exclude={"decision_id", "decision_binding_sha256"}
    )
    payload.update(
        binding_hash_version=2,
        change_impact_decision_digest=SEMANTIC_DIGEST,
        change_impact_policy_record_id=f"impact-policy-{revision}",
        change_impact_policy_revision=revision,
        change_impact_policy_digest=("a" if revision == 1 else "d") * 64,
    )
    return EngineeringReviewDecisionRecord.model_validate(payload)


class ChangeImpactBindingVersionTests(unittest.TestCase):
    def test_legacy_binding_event_and_engineering_identity_remain_exact(self) -> None:
        event = _owner_acceptance_event()
        self.assertEqual(
            (
                event.binding.binding_sha256,
                event.acceptance_id,
                event.event_id,
                owner_acceptance_event_replay_digest(event),
            ),
            (
                "f097fbaef0d458a1f66c325098f21b7d8a1eef8fc9a36068ca54308ee0a9f5d7",
                "owner-acceptance-f097fbaef0d458a1f66c325098f21b7d",
                "owner-acceptance-event-01a4aa0c300b593db640f5d57fe0b9f5",
                "81da7d6df9c5adc155aabce192bd2b424535118bc7715c3daf96e294b47c3bf3",
            ),
        )
        engineering = _engineering_decision()
        self.assertEqual(
            (engineering.decision_id, engineering.decision_binding_sha256),
            (
                "engineering-review-decision-5da7ffe0cdc854b7aeabae6f",
                "5da7ffe0cdc854b7aeabae6ff59de5104d6ddb21d852609316e5cebf566ff691",
            ),
        )
        for record in (event.binding, engineering):
            dumped = record.model_dump(mode="json", exclude_none=True)
            self.assertNotIn("binding_hash_version", dumped)
            self.assertNotIn("change_impact_decision_digest", dumped)
            self.assertEqual(type(record).model_validate(dumped), record)

    def test_v2_domains_require_explicit_complete_identity_and_preserve_legacy_separation(
        self,
    ) -> None:
        for record in (_owner_acceptance_event().binding, _engineering_decision()):
            payload = record.model_dump(
                mode="json", exclude={"binding_sha256", "decision_id", "decision_binding_sha256"}
            )
            for updates in (
                {"binding_hash_version": 2},
                {"change_impact_decision_digest": SEMANTIC_DIGEST},
                {"binding_hash_version": 2, "change_impact_decision_digest": "not-a-digest"},
                {"binding_hash_version": 3, "change_impact_decision_digest": SEMANTIC_DIGEST},
            ):
                with self.subTest(record=type(record).__name__, updates=updates):
                    with self.assertRaises(ValidationError):
                        type(record).model_validate(payload | updates)
        self.assertNotEqual(
            _owner_acceptance_event().binding.binding_sha256, _v2_event().binding.binding_sha256
        )
        self.assertNotEqual(
            _engineering_decision().decision_binding_sha256,
            _v2_engineering().decision_binding_sha256,
        )
        self.assertEqual(
            _v2_event().binding.binding_sha256, _v2_event(revision=2).binding.binding_sha256
        )
        self.assertEqual(
            _v2_engineering().decision_binding_sha256,
            _v2_engineering(revision=2).decision_binding_sha256,
        )
