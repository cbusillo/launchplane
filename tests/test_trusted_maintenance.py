import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceActorRule,
    TrustedMaintenanceAllowedEvent,
    TrustedMaintenanceEvidenceRecord,
    TrustedMaintenancePolicyRecord,
    trusted_maintenance_policy_digest,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.trusted_maintenance import (
    TrustedMaintenanceAuthorityError,
    TrustedMaintenanceEvidenceConflictError,
    TrustedMaintenanceGitHubEventFacts,
    TrustedMaintenancePolicyConflictError,
    TrustedMaintenancePolicySequenceError,
    TrustedMaintenanceRuleMatchError,
    apply_trusted_maintenance_policy,
    capture_trusted_maintenance_evidence,
    get_trusted_maintenance_policy_read_model,
    plan_trusted_maintenance_evidence_append,
    plan_trusted_maintenance_policy_append,
    trusted_maintenance_current_authority,
    trusted_maintenance_path_result,
    trusted_maintenance_path_result_from_store,
)


PRODUCT = "example-product"
CONTEXT = "example-context"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/example-product"
PULL_REQUEST_NUMBER = 17
HEAD_SHA = "a" * 40
OLDER_HEAD_SHA = "b" * 40
OCCURRED_AT = "2026-07-31T10:15:00Z"
EVALUATED_AT = "2026-07-31T10:20:00Z"


class TrustedMaintenanceTests(unittest.TestCase):
    def test_policy_ids_and_digests_are_deterministic_and_status_agnostic(self) -> None:
        active = _policy(actor_id=301, sender_ids=(301,), actor_login="audit-login-a")
        superseded = _policy(
            actor_id=301,
            sender_ids=(301,),
            actor_login="audit-login-b",
            sender_logins=("sender-audit",),
            status="superseded",
        )

        self.assertEqual(active.record_id, superseded.record_id)
        self.assertEqual(active.policy_digest, superseded.policy_digest)
        self.assertEqual(trusted_maintenance_policy_digest(superseded), active.policy_digest)
        self.assertIn(REPOSITORY_ID, active.record_id)

    def test_validation_requires_numeric_bot_actor_sender_and_explicit_events(self) -> None:
        with self.assertRaises(ValueError):
            _policy(actor_id=0)
        with self.assertRaisesRegex(ValueError, "sender_github_ids"):
            _policy(sender_ids=())
        with self.assertRaisesRegex(ValueError, "actor rule requires allowed_events"):
            _policy(allowed_events=())
        with self.assertRaisesRegex(ValueError, "Bot"):
            TrustedMaintenanceActorRule(
                actor_github_id=301,
                actor_type="User",  # type: ignore[arg-type]
                sender_github_ids=(301,),
                allowed_events=(_allowed_event(),),
            )

    def test_display_logins_are_audit_only_not_matching_authority(self) -> None:
        classification = _classification()
        policy = _policy(actor_id=301, sender_ids=(301,))
        event = _event_facts(
            pr_author_github_id=301,
            sender_github_id=301,
            pr_author_login="not-the-policy-login",
            sender_login="also-not-policy-login",
        )

        captured = capture_trusted_maintenance_evidence(
            candidate=_candidate(),
            classification=classification,
            policy_record=policy,
            event_facts=event,
            occurred_at=OCCURRED_AT,
            recorded_at=OCCURRED_AT,
        )

        self.assertEqual(captured.path_result.state, "satisfied")
        self.assertEqual(captured.record.binding.pr_author_login, "not-the-policy-login")

    def test_no_arbitrary_bot_bypass(self) -> None:
        with self.assertRaises(TrustedMaintenanceRuleMatchError):
            capture_trusted_maintenance_evidence(
                candidate=_candidate(),
                classification=_classification(),
                policy_record=_policy(actor_id=301, sender_ids=(301,)),
                event_facts=_event_facts(pr_author_github_id=999, sender_github_id=999),
                occurred_at=OCCURRED_AT,
                recorded_at=OCCURRED_AT,
            )

    def test_actor_sender_event_and_same_repo_head_match_exactly(self) -> None:
        policy = _policy(actor_id=301, sender_ids=(301, 302))
        classification = _classification()
        cases = {
            "wrong_actor": _event_facts(pr_author_github_id=302, sender_github_id=302),
            "wrong_sender": _event_facts(pr_author_github_id=301, sender_github_id=999),
            "wrong_event": _event_facts(
                pr_author_github_id=301,
                sender_github_id=301,
                event_name="issues",
            ),
            "wrong_action": _event_facts(
                pr_author_github_id=301,
                sender_github_id=301,
                event_action="closed",
            ),
            "wrong_type": _event_facts(
                pr_author_github_id=301,
                sender_github_id=301,
                pr_author_type="User",
            ),
            "fork_head": _event_facts(
                pr_author_github_id=301,
                sender_github_id=301,
                head_repository_id="1002",
                head_repository_owner_id="2002",
                head_repository="example/fork",
            ),
        }
        for name, event_facts in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(TrustedMaintenanceRuleMatchError):
                    capture_trusted_maintenance_evidence(
                        candidate=_candidate(),
                        classification=classification,
                        policy_record=policy,
                        event_facts=event_facts,
                        occurred_at=OCCURRED_AT,
                        recorded_at=OCCURRED_AT,
                    )

        captured = capture_trusted_maintenance_evidence(
            candidate=_candidate(),
            classification=classification,
            policy_record=policy,
            event_facts=_event_facts(pr_author_github_id=301, sender_github_id=302),
            occurred_at=OCCURRED_AT,
            recorded_at=OCCURRED_AT,
        )
        self.assertEqual(captured.path_result.state, "satisfied")
        self.assertEqual(
            captured.record.binding.matched_actor_rule_id, policy.actor_rules[0].rule_id
        )

    def test_evidence_requires_db_server_time_not_request_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "occurred_at=recorded_at"):
            capture_trusted_maintenance_evidence(
                candidate=_candidate(),
                classification=_classification(),
                policy_record=_policy(),
                event_facts=_event_facts(),
                occurred_at=OCCURRED_AT,
                recorded_at=EVALUATED_AT,
            )

        with self.assertRaisesRegex(TrustedMaintenanceAuthorityError, "classification"):
            capture_trusted_maintenance_evidence(
                candidate=_candidate(),
                classification=_classification(classified_at="2026-07-31T10:16:00Z"),
                policy_record=_policy(),
                event_facts=_event_facts(),
                occurred_at=OCCURRED_AT,
                recorded_at=OCCURRED_AT,
            )

    def test_current_authority_uses_repository_wide_highest_classification_first(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            revision_1 = _classification(revision=1)
            revision_2 = _classification(
                revision=2,
                repository_owner_id="2999",
                supersedes_record_id=revision_1.record_id,
            )
            store.write_tenant_repository_classification_record(revision_1)
            store.write_tenant_repository_classification_record(revision_2)
            store.write_trusted_maintenance_policy_record(_policy())

            with self.assertRaisesRegex(TrustedMaintenanceAuthorityError, "classification"):
                trusted_maintenance_current_authority(
                    store=store,
                    candidate=_candidate(),
                    evaluated_at=EVALUATED_AT,
                )

    def test_policy_append_cas_replay_and_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            revision_1 = _policy(actor_id=301)
            revision_2 = _policy(
                actor_id=302,
                revision=2,
                supersedes_record_id=revision_1.record_id,
            )
            applied_1 = apply_trusted_maintenance_policy(store=store, record=revision_1)

            with self.assertRaisesRegex(ValueError, "expected current record ID and digest"):
                apply_trusted_maintenance_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                )
            with self.assertRaises(TrustedMaintenancePolicyConflictError):
                apply_trusted_maintenance_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id="wrong",
                    expected_current_policy_digest=revision_1.policy_digest,
                )

            applied_2 = apply_trusted_maintenance_policy(
                store=store,
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_policy_digest=revision_1.policy_digest,
            )
            replay_2 = apply_trusted_maintenance_policy(
                store=store,
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_policy_digest=revision_1.policy_digest,
            )
            read_model = get_trusted_maintenance_policy_read_model(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
                store=store,
            )

        self.assertEqual(applied_1.status, "applied")
        self.assertEqual(applied_2.status, "applied")
        self.assertEqual(replay_2.status, "replayed")
        self.assertEqual(read_model.status, "available")
        self.assertEqual(read_model.current_record, revision_2)

    def test_policy_apply_uses_atomic_store_compare(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            with patch.object(
                store,
                "list_trusted_maintenance_policy_records",
                side_effect=AssertionError("apply must not perform a pre-write CAS read"),
            ):
                applied = apply_trusted_maintenance_policy(
                    store=store,
                    record=_policy(),
                )

        self.assertEqual(applied.status, "applied")

    def test_policy_append_rejects_sequence_gaps_and_scope_drift(self) -> None:
        revision_1 = _policy()
        with self.assertRaises(TrustedMaintenancePolicySequenceError):
            plan_trusted_maintenance_policy_append(
                records=(revision_1,),
                record=_policy(revision=3, supersedes_record_id=revision_1.record_id),
            )
        with self.assertRaises(TrustedMaintenancePolicySequenceError):
            plan_trusted_maintenance_policy_append(
                records=(revision_1,),
                record=_policy(revision=2, supersedes_record_id="wrong-record"),
            )
        with self.assertRaises(TrustedMaintenancePolicyConflictError):
            plan_trusted_maintenance_policy_append(
                records=(revision_1,),
                record=_policy(
                    revision=2,
                    repository_owner_id="2999",
                    supersedes_record_id=revision_1.record_id,
                ),
            )

    def test_evidence_append_replay_and_conflict(self) -> None:
        captured = _captured_evidence()
        replayed_with_new_audit_facts = _captured_evidence(
            event_facts=_event_facts(
                pr_author_login="renamed-automation",
                sender_login="renamed-sender",
            ),
            occurred_at="2026-07-31T10:16:00Z",
        )
        replay = plan_trusted_maintenance_evidence_append(
            records=(captured,),
            record=replayed_with_new_audit_facts,
        )
        conflicting = _captured_evidence(
            candidate=_candidate(head_sha=OLDER_HEAD_SHA),
        )

        self.assertEqual(replay.status, "replayed")
        self.assertEqual(captured.evidence_id, replayed_with_new_audit_facts.evidence_id)
        self.assertEqual(
            captured.binding.binding_sha256,
            replayed_with_new_audit_facts.binding.binding_sha256,
        )
        self.assertNotEqual(captured.evidence_digest, replayed_with_new_audit_facts.evidence_digest)
        with self.assertRaises(TrustedMaintenanceEvidenceConflictError):
            plan_trusted_maintenance_evidence_append(
                records=(captured,),
                record=conflicting,
            )

    def test_path_stales_on_head_policy_classification_actor_expiry_and_ambiguous_authority(
        self,
    ) -> None:
        classification = _classification()
        policy = _policy()
        captured = _captured_evidence(classification=classification, policy=policy)
        expiring_policy = _policy(evidence_ttl_seconds=60)
        expiring = _captured_evidence(classification=classification, policy=expiring_policy)

        self.assertEqual(
            trusted_maintenance_path_result(
                candidate=_candidate(),
                classification=classification,
                policy_record=policy,
                evidence_records=(captured,),
                evaluated_at=EVALUATED_AT,
            ).state,
            "satisfied",
        )
        self.assertEqual(
            trusted_maintenance_path_result(
                candidate=_candidate(head_sha=OLDER_HEAD_SHA),
                classification=classification,
                policy_record=policy,
                evidence_records=(captured,),
                evaluated_at=EVALUATED_AT,
            ).state,
            "stale",
        )
        extended_expiry = TrustedMaintenanceEvidenceRecord.model_validate(
            expiring.model_dump(mode="json")
            | {"expires_at": "2026-07-31T11:15:00Z", "evidence_digest": ""}
        )
        self.assertEqual(
            trusted_maintenance_path_result(
                candidate=_candidate(),
                classification=classification,
                policy_record=expiring_policy,
                evidence_records=(extended_expiry,),
                evaluated_at="2026-07-31T10:15:30Z",
            ).state,
            "stale",
        )
        self.assertEqual(
            trusted_maintenance_path_result(
                candidate=_candidate(),
                classification=_classification(
                    revision=2, supersedes_record_id=classification.record_id
                ),
                policy_record=policy,
                evidence_records=(captured,),
                evaluated_at=EVALUATED_AT,
            ).state,
            "stale",
        )
        self.assertEqual(
            trusted_maintenance_path_result(
                candidate=_candidate(),
                classification=classification,
                policy_record=_policy(actor_id=302),
                evidence_records=(captured,),
                evaluated_at=EVALUATED_AT,
            ).state,
            "stale",
        )
        self.assertEqual(
            trusted_maintenance_path_result(
                candidate=_candidate(),
                classification=classification,
                policy_record=expiring_policy,
                evidence_records=(expiring,),
                evaluated_at="2026-07-31T10:16:00Z",
            ).state,
            "stale",
        )

        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            record_dir = state_dir / "launchplane_trusted_maintenance_policies"
            record_dir.mkdir(parents=True)
            payload = policy.model_dump(mode="json")
            (record_dir / f"{policy.record_id}.json").write_text(
                _json_dump(payload),
                encoding="utf-8",
            )
            (record_dir / "duplicate.json").write_text(_json_dump(payload), encoding="utf-8")
            store = FilesystemRecordStore(state_dir)
            store.write_tenant_repository_classification_record(classification)

            with self.assertRaises(TrustedMaintenanceAuthorityError):
                trusted_maintenance_path_result_from_store(
                    store=store,
                    candidate=_candidate(),
                    evaluated_at=EVALUATED_AT,
                )

    def test_current_authority_rejects_orphan_active_policy_revision(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir)
            store.write_tenant_repository_classification_record(_classification())
            orphan = _policy(
                revision=2,
                supersedes_record_id="missing-policy-revision-1",
            )
            record_dir = state_dir / "launchplane_trusted_maintenance_policies"
            record_dir.mkdir(parents=True)
            (record_dir / f"{orphan.record_id}.json").write_text(
                _json_dump(orphan.model_dump(mode="json")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(TrustedMaintenanceAuthorityError, "policy history"):
                trusted_maintenance_current_authority(
                    store=store,
                    candidate=_candidate(),
                    evaluated_at=EVALUATED_AT,
                )

    def test_store_read_path_returns_trusted_maintenance_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            classification = _classification()
            policy = _policy()
            evidence = _captured_evidence(classification=classification, policy=policy)
            store.write_tenant_repository_classification_record(classification)
            store.write_trusted_maintenance_policy_record(policy)
            store.write_trusted_maintenance_evidence_record(evidence)

            path_result = trusted_maintenance_path_result_from_store(
                store=store,
                candidate=_candidate(),
                evaluated_at=EVALUATED_AT,
            )

        assert path_result is not None
        self.assertEqual(path_result.path_kind, "trusted_maintenance")
        self.assertEqual(path_result.state, "satisfied")


def _policy(
    *,
    actor_id: int = 301,
    actor_login: str = "automation-301",
    sender_ids: tuple[int, ...] = (301,),
    sender_logins: tuple[str, ...] = (),
    allowed_events: tuple[TrustedMaintenanceAllowedEvent, ...] | None = None,
    repository_id: str = REPOSITORY_ID,
    repository_owner_id: str = REPOSITORY_OWNER_ID,
    repository: str = REPOSITORY,
    product: str = PRODUCT,
    context: str = CONTEXT,
    status: str = "active",
    revision: int = 1,
    effective_at: str = "2026-07-31T10:00:00Z",
    evidence_ttl_seconds: int | None = None,
    reason: str = "test trusted maintenance policy",
    supersedes_record_id: str | None = None,
) -> TrustedMaintenancePolicyRecord:
    actor_rule = TrustedMaintenanceActorRule(
        actor_github_id=actor_id,
        actor_login=actor_login,
        sender_github_ids=sender_ids,
        sender_logins=sender_logins,
        allowed_events=allowed_events if allowed_events is not None else (_allowed_event(),),
    )
    return TrustedMaintenancePolicyRecord.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "repository": repository,
            "product": product,
            "context": context,
            "status": status,
            "policy_revision": revision,
            "actor_rules": (actor_rule,),
            "evidence_ttl_seconds": evidence_ttl_seconds,
            "effective_at": effective_at,
            "source": "test-source",
            "reason": reason,
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _allowed_event(
    *,
    event_name: str = "pull_request",
    actions: tuple[str, ...] = ("opened", "synchronize"),
) -> TrustedMaintenanceAllowedEvent:
    return TrustedMaintenanceAllowedEvent(event_name=event_name, actions=actions)


def _candidate(
    *,
    product: str = PRODUCT,
    context: str = CONTEXT,
    repository_id: str = REPOSITORY_ID,
    repository_owner_id: str = REPOSITORY_OWNER_ID,
    repository: str = REPOSITORY,
    pull_request_number: int = PULL_REQUEST_NUMBER,
    head_sha: str = HEAD_SHA,
) -> TenantMergeCandidate:
    return TenantMergeCandidate(
        product=product,
        context=context,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
    )


def _classification(
    *,
    repository_id: str = REPOSITORY_ID,
    repository_owner_id: str = REPOSITORY_OWNER_ID,
    repository: str = REPOSITORY,
    product: str = PRODUCT,
    context: str = CONTEXT,
    kind: str = "tenant_ui",
    revision: int = 1,
    classified_at: str = "2026-07-31T09:00:00Z",
    supersedes_record_id: str | None = None,
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "repository": repository,
            "product": product,
            "context": context,
            "classification_kind": kind,
            "classification_revision": revision,
            "classified_at": classified_at,
            "source": "test-classifier",
            "reason": "test classification",
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _event_facts(
    *,
    pr_author_github_id: int = 301,
    pr_author_type: str = "Bot",
    pr_author_login: str = "automation-301",
    sender_github_id: int = 301,
    sender_type: str = "Bot",
    sender_login: str = "automation-301",
    head_repository_id: str = REPOSITORY_ID,
    head_repository_owner_id: str = REPOSITORY_OWNER_ID,
    head_repository: str = REPOSITORY,
    event_name: str = "pull_request",
    event_action: str = "synchronize",
    delivery_id: str = "delivery-1001",
) -> TrustedMaintenanceGitHubEventFacts:
    return TrustedMaintenanceGitHubEventFacts(
        pr_author_github_id=pr_author_github_id,
        pr_author_type=pr_author_type,
        pr_author_login=pr_author_login,
        sender_github_id=sender_github_id,
        sender_type=sender_type,
        sender_login=sender_login,
        head_repository_id=head_repository_id,
        head_repository_owner_id=head_repository_owner_id,
        head_repository=head_repository,
        event_name=event_name,
        event_action=event_action,
        source="github-webhook",
        delivery_id=delivery_id,
    )


def _captured_evidence(
    *,
    candidate: TenantMergeCandidate | None = None,
    classification: TenantRepositoryClassificationRecord | None = None,
    policy: TrustedMaintenancePolicyRecord | None = None,
    event_facts: TrustedMaintenanceGitHubEventFacts | None = None,
    occurred_at: str = OCCURRED_AT,
) -> TrustedMaintenanceEvidenceRecord:
    captured = capture_trusted_maintenance_evidence(
        candidate=candidate or _candidate(),
        classification=classification or _classification(),
        policy_record=policy or _policy(),
        event_facts=event_facts or _event_facts(),
        occurred_at=occurred_at,
        recorded_at=occurred_at,
    )
    return captured.record


def _json_dump(payload: object) -> str:
    import json

    return json.dumps(payload, sort_keys=True)
