"""Legacy identity and inert v2 consumer compatibility across immutable review records."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.change_impact_service import evaluate_change_impact
from control_plane.contracts.change_impact import (
    ChangeImpactEvaluation,
    ChangeImpactTargetReference,
)
from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceBinding,
    OwnerAcceptanceDecision,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceTransitionError,
    owner_acceptance_event_replay_digest,
    owner_acceptance_event_replay_matches,
)
from control_plane.http_routes.owner_acceptance import _owner_acceptance_event_persistence_outcome
from control_plane.merge_admission_impact_binding import (
    impact_binding_fingerprints,
    select_current_engineering_decision,
)
from control_plane.merge_readiness import _engineering_review_facet
from control_plane.owner_acceptance import (
    OwnerAcceptanceEventConflictError,
    evaluate_owner_acceptance_for_binding,
    record_owner_acceptance_event,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_merge_readiness import _engineering_decision, _target
from tests.test_owner_acceptance import (
    REPOSITORY,
    _EvidenceProvider,
    _human,
    _repository_evidence,
    _store,
)
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

    def test_legacy_evaluator_never_produces_v2_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            evidence = _repository_evidence()
            policies = store.list_change_impact_policy_records(
                repository_id=evidence.target.repository_id
            )
            for policy_set in (policies, ()):
                impact = evaluate_change_impact(repository_evidence=evidence, policies=policy_set)
                self.assertIsNone(impact.binding_hash_version)
                self.assertIsNone(impact.change_impact_decision_digest)

    def test_replay_ignores_only_v2_policy_provenance(self) -> None:
        original, replay = _v2_event(), _v2_event(revision=2)
        self.assertTrue(owner_acceptance_event_replay_matches(original, replay))
        self.assertNotEqual(
            owner_acceptance_event_replay_digest(original),
            owner_acceptance_event_replay_digest(replay),
        )
        for changes in ({"reason": "different request"}, {"source_event_id": "different-key"}):
            payload = replay.model_dump(mode="json", exclude={"event_id"}) | changes
            changed = OwnerAcceptanceEventRecord.model_validate(payload)
            self.assertFalse(owner_acceptance_event_replay_matches(original, changed))
        actor_payload = replay.model_dump(mode="json")
        actor_payload["authorization"]["owner_github_id"] += 1
        changed_actor = OwnerAcceptanceEventRecord.model_validate(actor_payload)
        self.assertFalse(owner_acceptance_event_replay_matches(original, changed_actor))
        binding_payload = replay.binding.model_dump(mode="json", exclude={"binding_sha256"})
        changed_binding = OwnerAcceptanceBinding.model_validate(
            binding_payload | {"change_impact_decision_digest": "f" * 64}
        )
        changed_event = OwnerAcceptanceEventRecord.model_validate(
            replay.model_dump(mode="json", exclude={"event_id", "acceptance_id", "binding"})
            | {"binding": changed_binding}
        )
        self.assertFalse(owner_acceptance_event_replay_matches(original, changed_event))

    def test_filesystem_and_sqlite_replay_preserve_original_record_and_reject_reaffirmation(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            postgres = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{directory}/test.sqlite3"
            )
            postgres.ensure_schema()
            try:
                for store in (FilesystemRecordStore(Path(directory) / "state"), postgres):
                    with self.subTest(store=type(store).__name__):
                        event = _v2_event()
                        self.assertEqual(
                            store.write_owner_acceptance_event_record(event), "written"
                        )
                        original = store.read_owner_acceptance_event_record(event.event_id)
                        self.assertEqual(
                            store.write_owner_acceptance_event_record(_v2_event(revision=2)),
                            "replayed",
                        )
                        self.assertEqual(
                            store.read_owner_acceptance_event_record(event.event_id), original
                        )
                        self.assertEqual(original.subject_sequence, 1)
                        self.assertEqual(
                            _owner_acceptance_event_persistence_outcome(
                                store=store, record=_v2_event(revision=2)
                            ),
                            "persisted",
                        )
                        changed = OwnerAcceptanceEventRecord.model_validate(
                            _v2_event(revision=2).model_dump(mode="json") | {"reason": "changed"}
                        )
                        with self.assertRaises(OwnerAcceptanceEventConflictError):
                            store.write_owner_acceptance_event_record(changed)
                        reaffirmed = OwnerAcceptanceEventRecord.model_validate(
                            _v2_event(revision=2).model_dump(mode="json", exclude={"event_id"})
                            | {"source_event_id": "new-key"}
                        )
                        with self.assertRaises(OwnerAcceptanceTransitionError):
                            store.write_owner_acceptance_event_record(reaffirmed)
                original_engineering = _v2_engineering()
                self.assertEqual(
                    postgres.write_engineering_review_decision_record_if_absent(
                        original_engineering
                    ),
                    (original_engineering, True),
                )
                self.assertEqual(
                    postgres.write_engineering_review_decision_record_if_absent(
                        _v2_engineering(revision=2)
                    ),
                    (original_engineering, False),
                )
            finally:
                postgres.close()

    def test_concurrent_filesystem_same_key_different_provenance_preserves_one_event(self) -> None:
        with TemporaryDirectory() as directory:
            barrier = threading.Barrier(2)
            events = (_v2_event(), _v2_event(revision=2))

            def write(event: OwnerAcceptanceEventRecord) -> str:
                store = FilesystemRecordStore(Path(directory))
                barrier.wait(timeout=5)
                return store.write_owner_acceptance_event_record(event)

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = tuple(executor.map(write, events))
            self.assertCountEqual(outcomes, ("written", "replayed"))
            stored = FilesystemRecordStore(Path(directory)).list_owner_acceptance_event_records()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].subject_sequence, 1)
            self.assertIn(stored[0].binding, tuple(event.binding for event in events))

    def test_current_v2_requires_fresh_human_review_and_original_review_age_still_expires(
        self,
    ) -> None:
        legacy = _owner_acceptance_event().model_copy(update={"subject_sequence": 1})
        current = _v2_event().binding
        decision = evaluate_owner_acceptance_for_binding(
            binding=current, events=(legacy,), evaluated_at=legacy.occurred_at
        )
        self.assertEqual((decision.status, decision.reason_code), ("stale", "acceptance_stale"))
        accepted = _v2_event().model_copy(update={"subject_sequence": 2})
        later = _v2_event(revision=2).binding
        current_decision = evaluate_owner_acceptance_for_binding(
            binding=later, events=(legacy, accepted), evaluated_at=accepted.occurred_at
        )
        self.assertEqual(current_decision.status, "accepted")
        expired = evaluate_owner_acceptance_for_binding(
            binding=later, events=(accepted,), evaluated_at="2027-08-07T12:00:00Z"
        )
        self.assertEqual(expired.reason_code, "owner_review_expired")
        revoked = OwnerAcceptanceEventRecord.model_validate(
            accepted.model_dump(mode="json", exclude={"event_id"})
            | {
                "action": "revoked",
                "reason": "Withdrawn",
                "source_event_id": "revoke",
                "subject_sequence": 3,
            }
        )
        self.assertEqual(
            evaluate_owner_acceptance_for_binding(
                binding=later, events=(accepted, revoked), evaluated_at=accepted.occurred_at
            ).status,
            "revoked",
        )

    def test_service_replays_same_v2_key_after_policy_provenance_changes(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            revision = [1]

            def classify(**kwargs: object) -> ChangeImpactEvaluation:
                actual = evaluate_change_impact(**kwargs)  # type: ignore[arg-type]
                return ChangeImpactEvaluation.model_validate(
                    actual.model_dump(mode="json")
                    | {
                        "binding_hash_version": 2,
                        "change_impact_decision_digest": SEMANTIC_DIGEST,
                        "policy_record_id": f"changed-policy-{revision[0]}",
                        "policy_revision": revision[0],
                        "policy_digest": ("a" if revision[0] == 1 else "d") * 64,
                    }
                )

            with patch(
                "control_plane.owner_acceptance.evaluate_change_impact", side_effect=classify
            ):
                from control_plane.owner_acceptance import evaluate_owner_acceptance

                target = ChangeImpactTargetReference(
                    repository=REPOSITORY, pull_request_number=2022
                )
                decision = evaluate_owner_acceptance(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    evaluated_at="2026-08-07T12:00:00Z",
                )
                assert decision.binding is not None
                kwargs = dict(
                    store=store,
                    repository_evidence_provider=provider,
                    target=target,
                    identity=_human(),
                    action="accepted",
                    expected_binding_sha256=decision.binding.binding_sha256,
                    source_event_kind="browser_api",
                    source_event_id="same-key",
                )
                first = record_owner_acceptance_event(**kwargs, occurred_at="2026-08-07T12:00:00Z")  # type: ignore[arg-type]
                revision[0] = 2
                replay = record_owner_acceptance_event(**kwargs, occurred_at="2026-08-07T12:01:00Z")  # type: ignore[arg-type]
                self.assertEqual(
                    (replay.status, replay.record, replay.decision.status),
                    ("replayed", first.record, "accepted"),
                )
                assert replay.decision.binding is not None
                self.assertEqual(replay.decision.binding.change_impact_policy_revision, 2)
                self.assertEqual(replay.record.binding.change_impact_policy_revision, 1)

    def test_admission_compares_scoped_identity_and_rejects_mixed_versions(self) -> None:
        engineering = _v2_engineering()
        impact = ChangeImpactEvaluation(
            status="success",
            reason_code="change_impact_classified",
            target=engineering.target,
            policy_digest="d" * 64,
            binding_hash_version=2,
            change_impact_decision_digest=SEMANTIC_DIGEST,
        )
        owner = OwnerAcceptanceDecision(
            status="not_required",
            reason_code="engineering_only",
            evaluated_at=engineering.evaluated_at,
        )
        self.assertEqual(
            impact_binding_fingerprints(
                impact=impact, owner_decision=owner, engineering_decision=engineering
            ),
            (SEMANTIC_DIGEST, SEMANTIC_DIGEST),
        )
        self.assertIsNone(
            impact_binding_fingerprints(
                impact=impact, owner_decision=owner, engineering_decision=_engineering_decision()
            )[1]
        )
        changed = impact.model_copy(update={"change_impact_decision_digest": "f" * 64})
        self.assertEqual(
            impact_binding_fingerprints(
                impact=changed, owner_decision=owner, engineering_decision=engineering
            ),
            (SEMANTIC_DIGEST, "f" * 64),
        )

    def test_admission_requires_latest_hash_version_and_still_checks_exact_head(self) -> None:
        current = _v2_engineering()
        legacy = _engineering_decision()
        impact = ChangeImpactEvaluation(
            status="success",
            reason_code="change_impact_classified",
            target=current.target,
            binding_hash_version=2,
            change_impact_decision_digest=SEMANTIC_DIGEST,
        )
        self.assertEqual(
            select_current_engineering_decision(impact=impact, decisions=(current, legacy)), current
        )
        self.assertIsNone(
            select_current_engineering_decision(impact=impact, decisions=(legacy, current))
        )
        self.assertIsNone(select_current_engineering_decision(impact=impact, decisions=(legacy,)))
        facet = _engineering_review_facet(
            target=_target(pull_request_head_sha="f" * 40),
            decision=current,
            evidence=(),
        )
        self.assertIn("engineering_review_head_mismatch", facet.reason_codes)
        self.assertNotEqual(facet.state, "ready")
