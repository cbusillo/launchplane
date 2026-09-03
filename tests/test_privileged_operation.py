from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
    ManagedAuthzPolicySetExecutionEvidence,
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    ManagedMergeTrainPolicyImportAgentSummary,
    ManagedMergeTrainPolicyImportHumanEvidence,
    ManagedMergeTrainPolicyImportProposalInput,
    ManagedSecretReencryptionHumanEvidence,
    ManagedSecretReencryptionPlanInput,
    PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
    PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
    PRIVILEGED_SECRET_OPERATION_READ_ACTION,
    PrivilegedOperationActor,
    PrivilegedOperationApproval,
    PrivilegedOperationAgentActor,
    PrivilegedOperationConflictError,
    PrivilegedOperationEventRecord,
    PrivilegedOperationRecord,
    build_privileged_operation_id,
    build_privileged_operation_id_for_actor,
    privileged_operation_agent_summary,
    privileged_operation_evidence_digest_candidates,
    privileged_operation_evidence_digest,
    privileged_operation_pre_state_digest,
    privileged_operation_record_digest,
    privileged_operation_request_digest,
    privileged_operation_request_digest_candidates,
)
from control_plane.authz_grant_service import AuthzManagedPolicyDiff
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.privileged_operation_registry import (
    MANAGED_MERGE_TRAIN_POLICY_IMPORT_DESCRIPTOR,
    MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
    PrivilegedOperationPlannerError,
    RegisteredPrivilegedOperationDescriptor,
    list_privileged_operation_descriptors,
    plan_managed_merge_train_policy_import,
    plan_managed_secret_reencryption,
)
from control_plane.privileged_operation_service import (
    PrivilegedOperationSemanticReviewError,
    cancel_privileged_operation,
    create_privileged_operation_plan,
    create_typed_privileged_operation_plan,
    expire_privileged_operation_if_due,
    list_privileged_operations,
    privileged_operation_semantic_review,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy, action_safety
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import (
    LaunchplanePrivilegedOperationRow,
    PostgresRecordStore,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record


def _test_key(offset: int) -> str:
    return base64.urlsafe_b64encode(bytes((offset + index) % 256 for index in range(32))).decode()


def _record(*, created_at: str = "2026-08-22T20:00:00+00:00") -> PrivilegedOperationRecord:
    request = ManagedSecretReencryptionPlanInput(
        reason="Review canonical root migration",
        source_label="test-plan",
    )
    evidence = ManagedSecretReencryptionHumanEvidence(
        result_status="ok",
        plan_digest="a" * 64,
        configured_secret_count=3,
        rotation_candidate_count=2,
        unchanged_count=1,
        unreadable_secret_count=0,
        active_key_id="root-2026",
        retirement_blocked_key_ids=("legacy-root",),
        retirement_ready_key_ids=(),
        legacy_compatibility_key_loaded=True,
    )
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=123,
        login="operator",
    )
    return PrivilegedOperationRecord(
        operation_id=build_privileged_operation_id(
            github_id=actor.github_id,
            source_event_id="request-1",
        ),
        descriptor_id="managed-secret-reencryption",
        descriptor_version=1,
        safety_class="secret_backed",
        status="planned",
        source_event_id="request-1",
        requested_by=actor,
        request=request,
        request_digest=privileged_operation_request_digest(request),
        evidence=evidence,
        evidence_digest=privileged_operation_evidence_digest(evidence),
        created_at=created_at,
        updated_at=created_at,
        expires_at="2026-08-22T20:30:00+00:00",
    )


def _planned_event(record: PrivilegedOperationRecord) -> PrivilegedOperationEventRecord:
    return PrivilegedOperationEventRecord(
        operation_id=record.operation_id,
        sequence=1,
        action="planned",
        occurred_at=record.created_at,
        source_kind="browser_api",
        source_event_id=record.source_event_id,
        actor=record.requested_by,
        resulting_record_digest=privileged_operation_record_digest(record),
    )


def _failed_authz_policy_record() -> PrivilegedOperationRecord:
    request = ManagedAuthzPolicySetProposalInput(
        managed_set_id="test.policy-operation",
        desired_policy=LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "managed_set_id": "test.policy-operation",
                        "managed_rule_id": "policy-operation-reader",
                        "github_ids": [123],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_operation.read"],
                    }
                ],
            }
        ),
        reason="Review the exact managed policy plan.",
    )
    evidence = ManagedAuthzPolicySetHumanEvidence(
        result_status="ok",
        plan_digest="d" * 64,
        diff=AuthzManagedPolicyDiff(
            managed_set_id="test.policy-operation",
            previous_record_id="authz-policy-previous",
            previous_revision=1,
            candidate_revision=2,
            previous_policy_sha256="a" * 64,
            desired_policy_sha256="b" * 64,
            desired_set_sha256="c" * 64,
            plan_sha256="d" * 64,
        ),
    )
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=123,
        login="operator",
    )
    request_digest = privileged_operation_request_digest(request)
    evidence_digest = privileged_operation_evidence_digest(evidence)
    approval = PrivilegedOperationApproval(
        approver=actor,
        descriptor_id="managed-authz-policy-set",
        descriptor_version=1,
        request_digest=request_digest,
        evidence_digest=evidence_digest,
        plan_digest=evidence.plan_digest,
        pre_state_digest=privileged_operation_pre_state_digest(evidence),
        policy_record_id="authz-policy-approval",
        policy_revision=1,
        policy_sha256="e" * 64,
        policy_source="test-policy",
        managed_set_id="privileged-operations.policy-planning",
        managed_rule_id="human-policy-planner",
        expires_at="2026-08-22T20:30:00+00:00",
        reason="Reviewed the exact policy plan.",
        rollback_class="policy_cas",
    )
    return PrivilegedOperationRecord(
        operation_id=build_privileged_operation_id_for_actor(
            descriptor_id="managed-authz-policy-set",
            actor=actor,
            source_event_id="historical-policy-failure",
        ),
        descriptor_id="managed-authz-policy-set",
        descriptor_version=1,
        safety_class="policy_admin",
        status="execution_failed",
        source_event_id="historical-policy-failure",
        requested_by=actor,
        request=request,
        request_digest=request_digest,
        evidence=evidence,
        evidence_digest=evidence_digest,
        created_at="2026-08-22T20:00:00+00:00",
        updated_at="2026-08-22T20:10:00+00:00",
        expires_at="2026-08-22T20:30:00+00:00",
        approval=approval,
        execution=ManagedAuthzPolicySetExecutionEvidence(
            result_status="error",
            result_digest="f" * 64,
            changed=False,
            reconciliation_required=True,
            failure_code="policy_cas_conflict",
        ),
        terminal_at="2026-08-22T20:10:00+00:00",
        terminal_reason="Policy execution failed closed.",
    )


def _legacy_authz_policy_payload() -> dict[str, object]:
    record = _failed_authz_policy_record()
    full_payload = record.model_dump(mode="json")
    full_request = full_payload["request"]
    full_evidence = full_payload["evidence"]
    assert isinstance(full_request, dict)
    assert isinstance(full_evidence, dict)
    legacy_request = dict(full_request)
    legacy_request.pop("administrator_quorum_change")
    legacy_evidence = json.loads(json.dumps(full_evidence))
    legacy_diff = legacy_evidence["diff"]
    assert isinstance(legacy_diff, dict)
    for field_name in (
        "previous_administrator_quorum",
        "administrator_quorum",
        "administrator_quorum_changed",
        "strict_human_administrator_count",
        "quorum_satisfied",
        "solo_administration_active",
    ):
        legacy_diff.pop(field_name)

    payload = record.model_dump(mode="json", exclude_none=True)
    request = payload["request"]
    evidence = payload["evidence"]
    assert isinstance(request, dict)
    assert isinstance(evidence, dict)
    stored_diff = evidence["diff"]
    assert isinstance(stored_diff, dict)
    for field_name in (
        "previous_administrator_quorum",
        "administrator_quorum",
        "administrator_quorum_changed",
        "strict_human_administrator_count",
        "quorum_satisfied",
        "solo_administration_active",
    ):
        stored_diff.pop(field_name)
    request_digest = canonical_json_sha256(legacy_request)
    evidence_digest = canonical_json_sha256(legacy_evidence)
    payload["request_digest"] = request_digest
    payload["evidence_digest"] = evidence_digest
    approval = payload["approval"]
    assert isinstance(approval, dict)
    approval["request_digest"] = request_digest
    approval["evidence_digest"] = evidence_digest
    return payload


class PrivilegedOperationContractTests(unittest.TestCase):
    def test_historical_authz_request_digest_without_quorum_field_remains_valid(self) -> None:
        current_record = _failed_authz_policy_record()
        current_payload = current_record.model_dump(mode="json", exclude_none=True)
        legacy_payload = _legacy_authz_policy_payload()

        current_loaded = PrivilegedOperationRecord.model_validate(current_payload)
        legacy_loaded = PrivilegedOperationRecord.model_validate(legacy_payload)

        self.assertEqual(current_loaded.request_digest, current_record.request_digest)
        self.assertNotEqual(legacy_loaded.request_digest, current_record.request_digest)
        self.assertIn(
            legacy_loaded.request_digest,
            privileged_operation_request_digest_candidates(legacy_loaded.request),
        )
        self.assertIn(
            legacy_loaded.evidence_digest,
            privileged_operation_evidence_digest_candidates(legacy_loaded.evidence),
        )
        tampered_payload = json.loads(json.dumps(legacy_payload))
        tampered_payload["request_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "request_digest does not match"):
            PrivilegedOperationRecord.model_validate(tampered_payload)
        tampered_payload = json.loads(json.dumps(legacy_payload))
        tampered_payload["evidence_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "evidence_digest does not match"):
            PrivilegedOperationRecord.model_validate(tampered_payload)

    def test_historical_digest_candidates_require_exact_optional_defaults(self) -> None:
        record = _failed_authz_policy_record()
        assert isinstance(record.evidence, ManagedAuthzPolicySetHumanEvidence)
        quorum_request = record.request.model_copy(update={"administrator_quorum_change": 1})
        quorum_evidence = record.evidence.model_copy(
            update={"diff": record.evidence.diff.model_copy(update={"quorum_satisfied": True})}
        )

        self.assertEqual(len(privileged_operation_request_digest_candidates(quorum_request)), 1)
        self.assertEqual(len(privileged_operation_evidence_digest_candidates(quorum_evidence)), 1)

    def test_action_safety_is_intentional(self) -> None:
        self.assertEqual(action_safety(PRIVILEGED_SECRET_OPERATION_PLAN_ACTION), "secret_backed")
        self.assertEqual(action_safety(PRIVILEGED_SECRET_OPERATION_READ_ACTION), "secret_backed")
        self.assertEqual(action_safety(PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION), "secret_backed")
        self.assertEqual(action_safety(PRIVILEGED_OPERATION_SUMMARY_READ_ACTION), "read")
        self.assertEqual(action_safety(AUTHZ_POLICY_OPERATION_PROPOSE_ACTION), "policy_admin")
        self.assertEqual(action_safety(AUTHZ_POLICY_OPERATION_APPROVE_ACTION), "policy_admin")
        self.assertEqual(action_safety(MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION), "policy_admin")
        self.assertEqual(action_safety(MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION), "policy_admin")

    def test_secret_actor_json_and_id_remain_compatible_while_policy_ids_are_namespaced(
        self,
    ) -> None:
        record = _record()
        self.assertNotIn("principal_sha256", record.model_dump_json())
        self.assertEqual(
            build_privileged_operation_id_for_actor(
                descriptor_id="managed-secret-reencryption",
                actor=record.requested_by,
                source_event_id=record.source_event_id,
            ),
            record.operation_id,
        )
        policy_operation_id = build_privileged_operation_id_for_actor(
            descriptor_id="managed-authz-policy-set",
            actor=PrivilegedOperationAgentActor(principal_sha256="d" * 64),
            source_event_id=record.source_event_id,
        )
        merge_train_policy_operation_id = build_privileged_operation_id_for_actor(
            descriptor_id="managed-merge-train-policy-import",
            actor=PrivilegedOperationAgentActor(principal_sha256="d" * 64),
            source_event_id=record.source_event_id,
        )
        self.assertNotEqual(policy_operation_id, record.operation_id)
        self.assertNotEqual(merge_train_policy_operation_id, policy_operation_id)

    def test_agent_summary_is_counts_only(self) -> None:
        rendered = privileged_operation_agent_summary(_record()).model_dump_json()
        self.assertNotIn("active_key_id", rendered)
        self.assertNotIn("legacy-root", rendered)
        self.assertNotIn("request", rendered)
        self.assertIn('"rotation_candidate_count":2', rendered)

    def test_semantic_review_covers_all_registered_descriptors_without_authority(self) -> None:
        descriptors = {descriptor.descriptor_id for descriptor in list_privileged_operation_descriptors()}
        records = (
            _record(),
            _failed_authz_policy_record(),
            self._merge_train_policy_import_record(),
        )
        reviews = tuple(privileged_operation_semantic_review(record=record) for record in records)

        self.assertEqual({review.descriptor_id for review in reviews}, descriptors)
        self.assertEqual(
            {review.operation_class for review in reviews},
            {
                "managed_secret_reencryption",
                "managed_authz_policy_set",
                "managed_merge_train_policy_import",
            },
        )
        self.assertFalse(any(review.authorizes_execution for review in reviews))
        self.assertTrue(all(review.schema_version == 1 for review in reviews))

    def test_semantic_review_redacts_raw_sensitive_payloads(self) -> None:
        records = (
            _record(),
            _failed_authz_policy_record(),
            self._merge_train_policy_import_record(),
        )
        rendered = json.dumps(
            [
                privileged_operation_semantic_review(record=record).model_dump(mode="json")
                for record in records
            ],
            sort_keys=True,
        )

        for secret in (
            "root-2026",
            "legacy-root",
            "desired_policy",
            "github_ids",
            "operator",
            "principal_sha256",
            "cbusillo/codex-skills",
            "merge-train-policy-candidate",
            "LAUNCHPLANE_GITHUB_TOKEN",
            "source_event_id",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("desired_policy", _failed_authz_policy_record().model_dump_json())

    def test_semantic_review_activity_is_chronological_and_redacted(self) -> None:
        record = _record()
        later = _planned_event(record).model_copy(
            update={"sequence": 2, "occurred_at": "2026-08-22T20:05:00+00:00"}
        )
        earlier = _planned_event(record)
        review = privileged_operation_semantic_review(record=record, events=(later, earlier))

        self.assertEqual(
            tuple(entry.occurred_at for entry in review.activity),
            (
                "2026-08-22T20:00:00+00:00",
                "2026-08-22T20:05:00+00:00",
            ),
        )
        self.assertEqual(tuple(entry.actor_type for entry in review.activity), ("github_human",) * 2)
        self.assertNotIn("operator", review.model_dump_json())

    def test_semantic_review_fails_closed_on_descriptor_drift(self) -> None:
        record = _record().model_copy(update={"descriptor_version": 2})

        with self.assertRaisesRegex(PrivilegedOperationSemanticReviewError, "drifted"):
            privileged_operation_semantic_review(record=record)

    def test_semantic_review_fails_closed_on_unknown_registered_descriptor_drift(self) -> None:
        with patch(
            "control_plane.privileged_operation_service.read_privileged_operation_descriptor",
            side_effect=LookupError("unknown descriptor"),
        ):
            with self.assertRaisesRegex(
                PrivilegedOperationSemanticReviewError,
                "not registered",
            ):
                privileged_operation_semantic_review(record=_record())

    def _merge_train_policy_import_record(self) -> PrivilegedOperationRecord:
        request = ManagedMergeTrainPolicyImportProposalInput(
            record=build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-candidate",
                updated_at="2026-08-22T20:00:00+00:00",
            ),
            reason="Review exact merge-train policy import.",
            related_issue="cbusillo/launchplane#2296",
        )
        evidence = ManagedMergeTrainPolicyImportHumanEvidence(
            plan_digest="8" * 64,
            active_record_id="merge-train-policy-active",
            active_updated_at="2026-08-22T19:00:00+00:00",
            active_policy_sha256="9" * 64,
            active_target_count=1,
            candidate_record_id="merge-train-policy-candidate",
            candidate_policy_sha256="6" * 64,
            candidate_target_count=1,
            changed_policy_keys=("redacted/repository:main",),
        )
        actor = PrivilegedOperationAgentActor(principal_sha256="d" * 64)
        return PrivilegedOperationRecord(
            operation_id=build_privileged_operation_id_for_actor(
                descriptor_id="managed-merge-train-policy-import",
                actor=actor,
                source_event_id="merge-train-policy-request-1",
            ),
            descriptor_id="managed-merge-train-policy-import",
            descriptor_version=1,
            safety_class="policy_admin",
            status="planned",
            source_event_id="merge-train-policy-request-1",
            requested_by=actor,
            request=request,
            request_digest=privileged_operation_request_digest(request),
            evidence=evidence,
            evidence_digest=privileged_operation_evidence_digest(evidence),
            created_at="2026-08-22T20:00:00+00:00",
            updated_at="2026-08-22T20:00:00+00:00",
            expires_at="2026-08-22T20:30:00+00:00",
        )

    def test_secret_planner_calls_only_dry_run_and_discards_raw_errors(self) -> None:
        raw_result = {
            "status": "error",
            "dry_run": True,
            "plan_digest": "b" * 64,
            "rotated_count": 2,
            "unchanged_count": 1,
            "error_count": 1,
            "errors": ["Managed secret secret-private-id is not decryptable"],
            "active_key_id": "root-2026",
            "retirement_blocked_key_ids": ["legacy-root"],
            "retirement_ready_key_ids": [],
            "legacy_compatibility_key_loaded": True,
        }
        store = unittest.mock.Mock()
        store.list_secret_records = unittest.mock.Mock()
        store.read_secret_version = unittest.mock.Mock()
        with patch(
            "control_plane.privileged_operation_registry.control_plane_secrets.reencrypt_secrets",
            return_value=raw_result,
        ) as reencrypt:
            evidence = plan_managed_secret_reencryption(
                store,
                ManagedSecretReencryptionPlanInput(reason="Inspect root migration"),
            )
        self.assertFalse(reencrypt.call_args.kwargs["apply"])
        self.assertEqual(evidence.unreadable_secret_count, 1)
        self.assertNotIn("secret-private-id", evidence.model_dump_json())

    def test_merge_train_policy_import_planner_binds_redacted_target_change_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            active_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-active",
                updated_at="2026-08-22T19:00:00+00:00",
            )
            candidate_record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-candidate",
                updated_at="2026-08-22T20:00:00+00:00",
            )
            store.write_merge_train_policy_record(active_record)

            evidence = plan_managed_merge_train_policy_import(
                store,
                ManagedMergeTrainPolicyImportProposalInput(
                    record=candidate_record,
                    reason="Review exact merge-train policy enrollment.",
                    related_issue="cbusillo/launchplane#2296",
                ),
            )

        self.assertEqual(evidence.active_record_id, "merge-train-policy-active")
        self.assertEqual(evidence.active_updated_at, "2026-08-22T19:00:00+00:00")
        self.assertEqual(evidence.candidate_record_id, "merge-train-policy-candidate")
        self.assertEqual(evidence.added_policy_keys, ("cbusillo/codex-skills:main",))
        self.assertEqual(evidence.removed_policy_keys, ("cbusillo/sellyouroutboard:main",))
        rendered = evidence.model_dump_json()
        self.assertNotIn("GH_TOKEN", rendered)
        self.assertNotIn("launchplane-merge-train", rendered)

    def test_merge_train_policy_import_planner_rejects_changed_policy_with_reused_record_id(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            active_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-shared",
            )
            store.write_merge_train_policy_record(active_record)
            request = ManagedMergeTrainPolicyImportProposalInput(
                record=build_test_merge_train_policy_record(
                    repository="cbusillo/codex-skills",
                    record_id="merge-train-policy-shared",
                ),
                reason="Review exact merge-train policy enrollment.",
            )

            with self.assertRaisesRegex(
                PrivilegedOperationPlannerError,
                "must use a new record ID",
            ):
                plan_managed_merge_train_policy_import(store, request)

    def test_merge_train_policy_import_planner_rejects_record_id_from_history(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            historical_record = build_test_merge_train_policy_record(
                repository="cbusillo/odoo-devkit",
                record_id="merge-train-policy-historical",
            ).model_copy(update={"status": "superseded"})
            active_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-active",
            )
            store.write_merge_train_policy_record(historical_record)
            store.write_merge_train_policy_record(active_record)
            request = ManagedMergeTrainPolicyImportProposalInput(
                record=build_test_merge_train_policy_record(
                    repository="cbusillo/codex-skills",
                    record_id=historical_record.record_id,
                ),
                reason="Review exact merge-train policy enrollment.",
            )

            with self.assertRaisesRegex(
                PrivilegedOperationPlannerError,
                "already exists in policy history",
            ):
                plan_managed_merge_train_policy_import(store, request)

    def test_merge_train_policy_import_planner_fails_closed_on_history_read_error(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            active_record = build_test_merge_train_policy_record(
                repository="cbusillo/sellyouroutboard",
                record_id="merge-train-policy-active",
            )
            store.write_merge_train_policy_record(active_record)
            request = ManagedMergeTrainPolicyImportProposalInput(
                record=build_test_merge_train_policy_record(
                    repository="cbusillo/codex-skills",
                    record_id="merge-train-policy-candidate",
                ),
                reason="Review exact merge-train policy enrollment.",
            )

            with (
                patch.object(
                    store,
                    "read_merge_train_policy_record",
                    side_effect=OSError("storage unavailable"),
                ),
                self.assertRaisesRegex(
                    PrivilegedOperationPlannerError,
                    "could not verify candidate record history",
                ),
            ):
                plan_managed_merge_train_policy_import(store, request)

    def test_merge_train_policy_import_record_rejects_authz_policy_evidence(self) -> None:
        request = ManagedMergeTrainPolicyImportProposalInput(
            record=build_test_merge_train_policy_record(),
            reason="Review exact merge-train policy enrollment.",
        )
        evidence = ManagedMergeTrainPolicyImportHumanEvidence(
            plan_digest="c" * 64,
            active_record_id="active",
            active_updated_at="2026-08-22T19:00:00+00:00",
            active_policy_sha256="a" * 64,
            active_target_count=1,
            candidate_record_id="candidate",
            candidate_policy_sha256="b" * 64,
            candidate_target_count=1,
            changed_policy_keys=("cbusillo/sellyouroutboard:main",),
        )
        actor = PrivilegedOperationAgentActor(principal_sha256="d" * 64)
        record = PrivilegedOperationRecord(
            operation_id=build_privileged_operation_id_for_actor(
                descriptor_id="managed-merge-train-policy-import",
                actor=actor,
                source_event_id="merge-train-plan",
            ),
            descriptor_id="managed-merge-train-policy-import",
            descriptor_version=1,
            safety_class="policy_admin",
            status="planned",
            source_event_id="merge-train-plan",
            requested_by=actor,
            request=request,
            request_digest=privileged_operation_request_digest(request),
            evidence=evidence,
            evidence_digest=privileged_operation_evidence_digest(evidence),
            created_at="2026-08-22T20:00:00+00:00",
            updated_at="2026-08-22T20:00:00+00:00",
            expires_at="2026-08-22T20:30:00+00:00",
        )

        summary = privileged_operation_agent_summary(record)

        assert isinstance(summary, ManagedMergeTrainPolicyImportAgentSummary)
        self.assertEqual(summary.descriptor_id, "managed-merge-train-policy-import")
        self.assertEqual(summary.added_policy_keys, ())
        self.assertEqual(summary.changed_policy_keys, ("cbusillo/sellyouroutboard:main",))
        self.assertEqual(
            MANAGED_MERGE_TRAIN_POLICY_IMPORT_DESCRIPTOR.plan_action,
            MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
        )


class PrivilegedOperationStorageTests(unittest.TestCase):
    def _stores(self, root: Path) -> tuple[FilesystemRecordStore, PostgresRecordStore]:
        postgres = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'records.sqlite'}")
        postgres.ensure_schema()
        return FilesystemRecordStore(root / "state"), postgres

    def test_plan_replay_cancel_and_expiry_are_consistent_across_stores(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            stores = self._stores(Path(temporary_directory))
            try:
                for store in stores:
                    record = _record()
                    event = _planned_event(record)
                    self.assertEqual(
                        store.write_privileged_operation_plan(record, event), "written"
                    )
                    self.assertEqual(
                        store.write_privileged_operation_plan(record, event), "replayed"
                    )
                    cancelled = cancel_privileged_operation(
                        record_store=store,
                        operation_id=record.operation_id,
                        actor_github_id=456,
                        actor_login="reviewer",
                        source_event_id="cancel-1",
                        reason="Superseded by a newer plan",
                        now=lambda: datetime(2026, 8, 22, 20, 10, tzinfo=timezone.utc),
                    )
                    self.assertEqual(cancelled.record.status, "cancelled")
                    self.assertEqual(
                        tuple(
                            item.action
                            for item in store.list_privileged_operation_event_records(
                                operation_id=record.operation_id
                            )
                        ),
                        ("cancelled", "planned"),
                    )

                    expiring = _record(created_at="2026-08-22T19:00:00+00:00").model_copy(
                        update={
                            "operation_id": build_privileged_operation_id(
                                github_id=123,
                                source_event_id="request-expiry",
                            ),
                            "source_event_id": "request-expiry",
                            "expires_at": "2026-08-22T19:30:00+00:00",
                        }
                    )
                    expiring_event = _planned_event(expiring).model_copy(
                        update={
                            "source_event_id": "request-expiry",
                            "resulting_record_digest": privileged_operation_record_digest(expiring),
                        }
                    )
                    store.write_privileged_operation_plan(expiring, expiring_event)
                    expired = expire_privileged_operation_if_due(
                        record_store=store,
                        record=expiring,
                        now=lambda: datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc),
                    )
                    self.assertEqual(expired.status, "expired")
            finally:
                stores[1].close()

    def test_historical_authz_failure_lists_across_stores(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stores = self._stores(root)
            payload = _legacy_authz_policy_payload()
            operation_id = payload["operation_id"]
            created_at = payload["created_at"]
            updated_at = payload["updated_at"]
            expires_at = payload["expires_at"]
            assert isinstance(operation_id, str)
            assert isinstance(created_at, str)
            assert isinstance(updated_at, str)
            assert isinstance(expires_at, str)
            try:
                filesystem_path = (
                    root / "state" / "launchplane_privileged_operations" / f"{operation_id}.json"
                )
                filesystem_path.parent.mkdir(parents=True, exist_ok=True)
                filesystem_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                postgres = stores[1]
                with postgres._session_factory() as session:  # noqa: SLF001
                    session.add(
                        LaunchplanePrivilegedOperationRow(
                            operation_id=operation_id,
                            descriptor_id="managed-authz-policy-set",
                            status="execution_failed",
                            requester_github_id=123,
                            created_at=created_at,
                            updated_at=updated_at,
                            expires_at=expires_at,
                            payload=payload,
                        )
                    )
                    session.commit()

                for store in stores:
                    records = store.list_privileged_operation_records(
                        status="execution_failed",
                        descriptor_id="managed-authz-policy-set",
                        limit=50,
                    )
                    self.assertEqual(
                        tuple(record.operation_id for record in records), (operation_id,)
                    )
            finally:
                stores[1].close()

    def test_service_replays_historical_authz_request_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            store = FilesystemRecordStore(state_dir)
            payload = _legacy_authz_policy_payload()
            record = PrivilegedOperationRecord.model_validate(payload)
            record_path = (
                state_dir / "launchplane_privileged_operations" / f"{record.operation_id}.json"
            )
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            planned_record = record.model_copy(
                update={
                    "status": "planned",
                    "updated_at": record.created_at,
                    "approval": None,
                    "execution": None,
                    "terminal_at": "",
                    "terminal_reason": "",
                }
            )
            event = _planned_event(planned_record)
            event_path = (
                state_dir / "launchplane_privileged_operation_events" / f"{event.event_id}.json"
            )
            event_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.write_text(
                json.dumps(
                    event.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True
                ),
                encoding="utf-8",
            )

            replay = create_typed_privileged_operation_plan(
                record_store=store,
                descriptor_id="managed-authz-policy-set",
                actor=record.requested_by,
                source_kind="browser_api",
                source_event_id=record.source_event_id,
                request=record.request,
                expires_in_seconds=30 * 60,
            )

            self.assertEqual(replay.write_status, "replayed")
            self.assertEqual(replay.record.request_digest, record.request_digest)

    def test_plan_replay_rejects_changed_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            record = _record()
            event = _planned_event(record)
            store.write_privileged_operation_plan(record, event)
            changed = record.model_copy(
                update={
                    "expires_at": (
                        datetime.fromisoformat(record.expires_at) + timedelta(minutes=5)
                    ).isoformat()
                }
            )
            with self.assertRaises(PrivilegedOperationConflictError):
                store.write_privileged_operation_plan(changed, event)

    def test_service_replays_plan_without_rerunning_planner(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            planner = unittest.mock.Mock(return_value=_record().evidence)
            registration = RegisteredPrivilegedOperationDescriptor(
                descriptor=MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
                planner=planner,
            )
            request = ManagedSecretReencryptionPlanInput(reason="Inspect root migration")
            with patch(
                "control_plane.privileged_operation_service.read_privileged_operation_descriptor",
                return_value=registration,
            ):
                first = create_privileged_operation_plan(
                    record_store=store,
                    requester_github_id=123,
                    requester_login="operator",
                    source_event_id="request-replay",
                    request=request,
                    now=lambda: datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc),
                )
                replay = create_privileged_operation_plan(
                    record_store=store,
                    requester_github_id=123,
                    requester_login="operator",
                    source_event_id="request-replay",
                    request=request,
                    now=lambda: datetime(2026, 8, 22, 20, 5, tzinfo=timezone.utc),
                )

            self.assertEqual(first.write_status, "written")
            self.assertEqual(replay.write_status, "replayed")
            self.assertEqual(replay.record, first.record)
            planner.assert_called_once()

    def test_service_replays_cancellation_with_same_source_event(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            record = _record()
            store.write_privileged_operation_plan(record, _planned_event(record))
            first = cancel_privileged_operation(
                record_store=store,
                operation_id=record.operation_id,
                actor_github_id=456,
                actor_login="reviewer",
                source_event_id="cancel-replay",
                reason="Superseded by a newer plan",
                now=lambda: datetime(2026, 8, 22, 20, 10, tzinfo=timezone.utc),
            )
            replay = cancel_privileged_operation(
                record_store=store,
                operation_id=record.operation_id,
                actor_github_id=456,
                actor_login="reviewer",
                source_event_id="cancel-replay",
                reason="Superseded by a newer plan",
                now=lambda: datetime(2026, 8, 22, 20, 20, tzinfo=timezone.utc),
            )

            self.assertEqual(first.write_status, "written")
            self.assertEqual(replay.write_status, "replayed")
            self.assertEqual(replay.record, first.record)

    def test_status_filter_applies_before_limit(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(Path(temporary_directory))
            cancelled_record = _record(created_at="2026-08-22T19:00:00+00:00").model_copy(
                update={"expires_at": "2026-08-22T19:30:00+00:00"}
            )
            store.write_privileged_operation_plan(
                cancelled_record,
                _planned_event(cancelled_record),
            )
            cancel_privileged_operation(
                record_store=store,
                operation_id=cancelled_record.operation_id,
                actor_github_id=456,
                actor_login="reviewer",
                source_event_id="cancel-filter",
                reason="Use the newer plan",
                now=lambda: datetime(2026, 8, 22, 19, 10, tzinfo=timezone.utc),
            )
            newer_record = _record(created_at="2026-08-22T20:00:00+00:00").model_copy(
                update={
                    "operation_id": build_privileged_operation_id(
                        github_id=123,
                        source_event_id="request-newer",
                    ),
                    "source_event_id": "request-newer",
                }
            )
            store.write_privileged_operation_plan(newer_record, _planned_event(newer_record))

            records = list_privileged_operations(
                record_store=store,
                status="cancelled",
                limit=1,
            )

            self.assertEqual(
                records, (store.read_privileged_operation_record(cancelled_record.operation_id),)
            )
