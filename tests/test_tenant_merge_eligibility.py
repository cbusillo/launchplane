import unittest

from pydantic import ValidationError

from control_plane.contracts.tenant_merge_eligibility import (
    TenantManagerPreviewApprovalEvidence,
    TenantMergeCandidate,
    TenantMergeEligibilityEvidenceInputs,
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationRecord,
    TenantTechnicalHumanWaiverEvidence,
    TenantTrustedMaintenanceEvidence,
    evaluate_tenant_merge_eligibility,
)


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"
PULL_REQUEST_NUMBER = 17
HEAD_SHA = "a" * 40
OLDER_HEAD_SHA = "b" * 40
EVALUATED_AT = "2026-07-31T12:00:00Z"
CLASSIFIED_AT = "2026-07-31T11:00:00Z"
BINDING_SHA256 = "c" * 64


class TenantMergeEligibilityTests(unittest.TestCase):
    def test_engineering_repository_is_admitted_through_normal_flow(self) -> None:
        classification = _classification(kind="engineering", revision=4)

        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(classification),
            evaluated_at=EVALUATED_AT,
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "engineering_normal_flow")
        self.assertEqual(decision.repository_id, REPOSITORY_ID)
        self.assertEqual(decision.repository_owner_id, REPOSITORY_OWNER_ID)
        self.assertEqual(decision.repository, REPOSITORY)
        self.assertEqual(decision.pull_request_number, PULL_REQUEST_NUMBER)
        self.assertEqual(decision.head_sha, HEAD_SHA)
        self.assertEqual(decision.classification_kind, "engineering")
        self.assertEqual(decision.classification_revision, 4)
        self.assertEqual(decision.classification_digest, classification.classification_digest)
        self.assertEqual(decision.evidence_kind, "none")
        self.assertEqual(len(decision.decision_binding_sha256), 64)

    def test_tenant_ui_defaults_to_manager_preview_required(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "manager_preview_required")
        self.assertEqual(decision.classification_kind, "tenant_ui")
        self.assertEqual(decision.evidence_kind, "none")

    def test_trusted_maintenance_takes_precedence_over_other_evidence(self) -> None:
        trusted = _trusted_maintenance()
        waiver = _technical_waiver()
        approval = _manager_approval(status="approved")

        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                trusted_maintenance=trusted,
                technical_human_waiver=waiver,
                manager_preview_approval=approval,
            ),
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "trusted_maintenance_admitted")
        self.assertEqual(decision.evidence_kind, "trusted_maintenance")
        self.assertEqual(decision.evidence_id, trusted.evidence_id)
        self.assertEqual(decision.evidence_digest, trusted.evidence_digest)

    def test_exact_head_technical_human_waiver_precedes_manager_approval(self) -> None:
        waiver = _technical_waiver()
        approval = _manager_approval(status="approved")

        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                trusted_maintenance=_trusted_maintenance(decision="not_trusted"),
                technical_human_waiver=waiver,
                manager_preview_approval=approval,
            ),
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "technical_human_waiver_admitted")
        self.assertEqual(decision.evidence_kind, "technical_human_waiver")
        self.assertEqual(decision.evidence_id, waiver.waiver_id)
        self.assertEqual(decision.evidence_digest, waiver.waiver_digest)

    def test_exact_manager_preview_approval_admits_tenant_ui_repository(self) -> None:
        classification = _classification(kind="tenant_ui", revision=7)
        approval = _manager_approval(status="approved")

        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(classification),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                manager_preview_approval=approval,
            ),
        )

        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "manager_preview_approved")
        self.assertEqual(decision.classification_revision, 7)
        self.assertEqual(decision.classification_digest, classification.classification_digest)
        self.assertEqual(decision.evidence_kind, "manager_preview_approval")
        self.assertEqual(decision.evidence_id, approval.approval_id)
        self.assertEqual(decision.evidence_digest, approval.approval_digest)
        self.assertEqual(len(decision.decision_binding_sha256), 64)

    def test_exact_head_drift_blocks_stale_technical_waiver(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                technical_human_waiver=_technical_waiver(head_sha=OLDER_HEAD_SHA),
            ),
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "evidence_head_mismatch")
        self.assertEqual(decision.evidence_kind, "none")

    def test_exact_head_drift_blocks_stale_manager_preview_approval(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                manager_preview_approval=_manager_approval(
                    head_sha=OLDER_HEAD_SHA,
                    status="approved",
                ),
            ),
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "evidence_head_mismatch")

    def test_non_current_evidence_fails_closed_when_no_later_evidence_admits(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                trusted_maintenance=_trusted_maintenance(current=False),
            ),
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "evidence_stale")

    def test_unknown_missing_ambiguous_and_stale_classification_fail_closed(self) -> None:
        cases = {
            "unknown": (
                TenantRepositoryClassificationLookup(status="unknown"),
                "classification_unknown",
            ),
            "missing": (
                TenantRepositoryClassificationLookup(status="missing"),
                "classification_missing",
            ),
            "ambiguous": (
                _lookup(
                    _classification(kind="tenant_ui", revision=1),
                    _classification(kind="tenant_ui", revision=2),
                ),
                "classification_ambiguous",
            ),
            "stale": (
                _lookup(_classification(kind="tenant_ui", current=False)),
                "classification_stale",
            ),
        }
        for label, (lookup, reason_code) in cases.items():
            with self.subTest(label=label):
                decision = evaluate_tenant_merge_eligibility(
                    candidate=_candidate(),
                    classification_lookup=lookup,
                    evaluated_at=EVALUATED_AT,
                    evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                        manager_preview_approval=_manager_approval(status="approved"),
                    ),
                )
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.reason_code, reason_code)

    def test_classification_identity_drift_fails_closed_even_when_name_matches(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(repository_id="9999"),
            classification_lookup=_lookup(_classification(kind="engineering")),
            evaluated_at=EVALUATED_AT,
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "classification_identity_drift")

    def test_repository_rename_drift_fails_closed_even_when_repository_id_matches(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(repository="example/renamed-tenant-site"),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "classification_identity_drift")

    def test_forbidden_heuristic_inputs_are_not_part_of_the_contract(self) -> None:
        with self.assertRaises(ValidationError):
            TenantMergeCandidate.model_validate(
                {
                    "repository_id": REPOSITORY_ID,
                    "repository_owner_id": REPOSITORY_OWNER_ID,
                    "repository": "example/tenant-ui-manager-approved",
                    "pull_request_number": PULL_REQUEST_NUMBER,
                    "head_sha": HEAD_SHA,
                    "labels": ["manager-approved"],
                    "changed_files": ["addons/example/views.xml"],
                    "pull_request_body": "manager approved",
                }
            )

        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(repository="example/engineering-platform"),
            classification_lookup=TenantRepositoryClassificationLookup(status="missing"),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                manager_preview_approval=_manager_approval(status="approved"),
            ),
        )
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "classification_missing")

    def test_classification_kinds_are_exactly_engineering_and_tenant_ui(self) -> None:
        for invalid_kind in ("tenant", "tenant_website", "shared_addon"):
            with self.subTest(kind=invalid_kind):
                with self.assertRaises(ValidationError):
                    TenantRepositoryClassificationRecord.model_validate(
                        {
                            "repository_id": REPOSITORY_ID,
                            "repository_owner_id": REPOSITORY_OWNER_ID,
                            "repository": REPOSITORY,
                            "classification_kind": invalid_kind,
                            "classification_revision": 1,
                            "classified_at": CLASSIFIED_AT,
                        }
                    )

    def test_agents_cannot_create_or_select_technical_human_waivers(self) -> None:
        for field_name in ("created_by_subject_kind", "selected_by_subject_kind"):
            payload = _technical_waiver().model_dump(mode="json")
            payload[field_name] = "terminal_agent"
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    TenantTechnicalHumanWaiverEvidence.model_validate(payload)

    def test_evidence_identity_drift_fails_closed(self) -> None:
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(_classification(kind="tenant_ui")),
            evaluated_at=EVALUATED_AT,
            evidence_inputs=TenantMergeEligibilityEvidenceInputs(
                trusted_maintenance=_trusted_maintenance(repository_id="9999"),
            ),
        )

        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "evidence_identity_drift")


def _candidate(**overrides: object) -> TenantMergeCandidate:
    payload = {
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "pull_request_number": PULL_REQUEST_NUMBER,
        "head_sha": HEAD_SHA,
    }
    payload.update(overrides)
    return TenantMergeCandidate.model_validate(payload)


def _classification(
    *,
    kind: str,
    revision: int = 1,
    current: bool = True,
    **overrides: object,
) -> TenantRepositoryClassificationRecord:
    payload = {
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "classification_kind": kind,
        "classification_revision": revision,
        "classified_at": CLASSIFIED_AT,
        "current": current,
    }
    payload.update(overrides)
    return TenantRepositoryClassificationRecord.model_validate(payload)


def _lookup(
    *records: TenantRepositoryClassificationRecord,
) -> TenantRepositoryClassificationLookup:
    return TenantRepositoryClassificationLookup(records=records)


def _trusted_maintenance(
    *,
    decision: str = "trusted_maintenance",
    current: bool = True,
    **scope_overrides: object,
) -> TenantTrustedMaintenanceEvidence:
    return TenantTrustedMaintenanceEvidence.model_validate(
        {
            "evidence_id": "trusted-maintenance-17",
            "scope": _candidate(**scope_overrides).model_dump(mode="json"),
            "decision": decision,
            "decided_at": EVALUATED_AT,
            "current": current,
        }
    )


def _technical_waiver(**scope_overrides: object) -> TenantTechnicalHumanWaiverEvidence:
    return TenantTechnicalHumanWaiverEvidence.model_validate(
        {
            "waiver_id": "technical-waiver-17",
            "scope": _candidate(**scope_overrides).model_dump(mode="json"),
            "authorized_human_github_id": 302,
            "authorized_human_login": "release-manager",
            "authorized_at": EVALUATED_AT,
        }
    )


def _manager_approval(
    *,
    status: str,
    current: bool = True,
    **scope_overrides: object,
) -> TenantManagerPreviewApprovalEvidence:
    return TenantManagerPreviewApprovalEvidence.model_validate(
        {
            "approval_id": "manager-preview-approval-17",
            "scope": _candidate(**scope_overrides).model_dump(mode="json"),
            "status": status,
            "binding_sha256": BINDING_SHA256,
            "evaluated_at": EVALUATED_AT,
            "current": current,
            "event_id": "manager-preview-event-17",
        }
    )


if __name__ == "__main__":
    unittest.main()
