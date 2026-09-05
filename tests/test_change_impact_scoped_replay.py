from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.change_impact_service import (
    evaluate_change_impact,
    load_change_impact_stored_evidence,
)
from control_plane.contracts.change_impact import (
    ChangeImpactPolicyRecord,
    ChangeImpactTargetReference,
)
from control_plane.merge_admission_impact_binding import impact_binding_fingerprints
from control_plane.owner_acceptance import evaluate_owner_acceptance, record_owner_acceptance_event
from tests.test_owner_acceptance import (
    REPOSITORY,
    _EvidenceProvider,
    _human,
    _repository_evidence,
    _store,
)


class ScopedImpactReplayTests(unittest.TestCase):
    def test_actual_v2_producer_preserves_owner_replay_and_admission_after_irrelevant_revision(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            current = store.list_change_impact_policy_records()[0]
            payload = current.model_dump(exclude={"record_id", "policy_digest"})
            payload.update(
                classification_model="v2",
                policy_revision=2,
                supersedes_record_id=current.record_id,
                component_rules=[
                    rule.model_dump(exclude={"rule_id"})
                    | {"product_impact": None if rule.affected_products else "declared_none"}
                    for rule in current.component_rules
                ],
            )
            v2 = ChangeImpactPolicyRecord.model_validate(payload)
            # Test storage fixture only: production v2 apply remains explicitly blocked.
            store.compare_and_write_change_impact_policy_record(
                v2,
                expected_current_record_id=current.record_id,
                expected_current_policy_digest=current.policy_digest,
            )
            evidence = _repository_evidence()
            evidence = evidence.model_copy(
                update={
                    "changed_files": tuple(
                        file.model_copy(update={"change_kind": "modified"})
                        for file in evidence.changed_files
                    )
                }
            )
            provider = _EvidenceProvider(evidence)
            target = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
            pending = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                evaluated_at="2026-08-07T12:00:00Z",
            )
            assert pending.binding is not None
            first = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=pending.binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="actual-producer-replay",
                occurred_at="2026-08-07T12:00:00Z",
            )
            revision = ChangeImpactPolicyRecord.model_validate(
                v2.model_dump(exclude={"record_id", "policy_digest"})
                | {
                    "policy_revision": 3,
                    "supersedes_record_id": v2.record_id,
                    "reason": "Provenance-only revision.",
                }
            )
            store.compare_and_write_change_impact_policy_record(
                revision,
                expected_current_record_id=v2.record_id,
                expected_current_policy_digest=v2.policy_digest,
            )
            replay = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=target,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=pending.binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="actual-producer-replay",
                occurred_at="2026-08-07T12:01:00Z",
            )
            self.assertEqual(
                (replay.status, replay.record, replay.decision.status),
                ("replayed", first.record, "accepted"),
            )
            assert replay.decision.binding is not None
            self.assertEqual(replay.decision.binding.change_impact_policy_revision, 3)
            self.assertEqual(replay.record.binding.change_impact_policy_revision, 2)
            impact = evaluate_change_impact(
                repository_evidence=evidence,
                policies=(revision,),
                stored_evidence=load_change_impact_stored_evidence(
                    store=store, target=evidence.target
                ),
            )
            digest = impact.change_impact_decision_digest
            self.assertIsNotNone(digest)
            self.assertEqual(
                impact_binding_fingerprints(
                    impact=impact, owner_decision=replay.decision, engineering_decision=None
                ),
                (digest, digest),
            )
