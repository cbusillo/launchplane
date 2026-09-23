import unittest

from pydantic import ValidationError

from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathResult,
    TenantMergeCandidate,
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationRecord,
    build_tenant_repository_classification_record_id,
    evaluate_tenant_merge_eligibility,
)

PRODUCT = "launchplane"
CONTEXT = "production"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"
PULL_REQUEST_NUMBER = 17
HEAD_SHA = "a" * 40
OLDER_HEAD_SHA = "b" * 40
EVALUATED_AT = "2026-07-31T12:00:00Z"
CLASSIFIED_AT = "2026-07-31T11:00:00Z"
SOURCE = "manual"
REASON = "initial classification"


class TenantMergeEligibilityTests(unittest.TestCase):
    def test_append_only_revision_ids(self) -> None:
        rec1 = _classification(revision=1)
        rec2 = _classification(revision=2)
        expected_id1 = build_tenant_repository_classification_record_id(
            repository_id=REPOSITORY_ID, classification_revision=1
        )
        expected_id2 = build_tenant_repository_classification_record_id(
            repository_id=REPOSITORY_ID, classification_revision=2
        )
        self.assertEqual(rec1.record_id, expected_id1)
        self.assertEqual(rec2.record_id, expected_id2)
        self.assertIn("1001", rec1.record_id)
        self.assertIn("r1", rec1.record_id)
        self.assertIn("r2", rec2.record_id)
        self.assertNotEqual(rec1.record_id, rec2.record_id)

    def test_highest_revision_selection(self) -> None:
        rev1 = _classification(kind="tenant_ui", revision=1)
        rev3 = _classification(kind="engineering", revision=3)
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(rev1, rev3),
            evaluated_at=EVALUATED_AT,
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.classification_kind, "engineering")
        self.assertEqual(decision.classification_revision, 3)

    def test_duplicate_highest_ambiguous(self) -> None:
        rev5a = _classification(kind="tenant_ui", revision=5, source="source-a")
        rev5b = _classification(kind="engineering", revision=5, source="source-b")
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(rev5a, rev5b),
            evaluated_at=EVALUATED_AT,
        )
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reason_code, "classification_ambiguous")

    def test_product_context_repository_identity_drift(self) -> None:
        cls = _classification(kind="engineering")
        cases = {
            "product_drift": _candidate(product="other_product"),
            "context_drift": _candidate(context="other_context"),
            "repository_id_drift": _candidate(repository_id="9999"),
            "repository_drift": _candidate(repository="example/other-site"),
        }
        for name, candidate in cases.items():
            with self.subTest(case=name):
                decision = evaluate_tenant_merge_eligibility(
                    candidate=candidate,
                    classification_lookup=_lookup(cls),
                    evaluated_at=EVALUATED_AT,
                )
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.reason_code, "classification_identity_drift")

    def test_engineering_fast_path(self) -> None:
        cls = _classification(kind="engineering")
        decision = evaluate_tenant_merge_eligibility(
            candidate=_candidate(),
            classification_lookup=_lookup(cls),
            evaluated_at=EVALUATED_AT,
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.reason_code, "engineering_normal_flow")
        self.assertEqual(decision.evidence_kind, "none")

    def test_forbidden_heuristic_inputs(self) -> None:
        with self.assertRaises(ValidationError):
            TenantMergeCandidate.model_validate(
                {
                    "product": PRODUCT,
                    "context": CONTEXT,
                    "repository_id": REPOSITORY_ID,
                    "repository_owner_id": REPOSITORY_OWNER_ID,
                    "repository": REPOSITORY,
                    "pull_request_number": PULL_REQUEST_NUMBER,
                    "head_sha": HEAD_SHA,
                    "labels": ["manager-approved"],
                }
            )

        with self.assertRaises(ValidationError):
            TenantAdmissionPathResult.model_validate(
                {
                    "path_kind": "manager_preview_approval",
                    "state": "satisfied",
                    "evidence_id": "mgr-1",
                    "evidence_digest": "d" * 64,
                    "repository_id": REPOSITORY_ID,
                    "repository_owner_id": REPOSITORY_OWNER_ID,
                    "repository": REPOSITORY,
                    "pull_request_number": PULL_REQUEST_NUMBER,
                    "head_sha": HEAD_SHA,
                    "classification_digest": "c" * 64,
                    "heuristic_signal": True,
                }
            )

    def test_only_two_classification_kinds(self) -> None:
        for invalid_kind in ("tenant", "shared_addon", "tenant_website"):
            with self.subTest(kind=invalid_kind):
                with self.assertRaises(ValidationError):
                    _classification(kind=invalid_kind)

    def test_deterministic_decision_digest(self) -> None:
        cls = _classification(kind="engineering")
        cand1 = _candidate()
        cand2 = _candidate(head_sha="b" * 40)

        d1 = evaluate_tenant_merge_eligibility(
            candidate=cand1,
            classification_lookup=_lookup(cls),
            evaluated_at=EVALUATED_AT,
        )
        d1_repeat = evaluate_tenant_merge_eligibility(
            candidate=cand1,
            classification_lookup=_lookup(cls),
            evaluated_at=EVALUATED_AT,
        )
        d2 = evaluate_tenant_merge_eligibility(
            candidate=cand2,
            classification_lookup=_lookup(cls),
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(d1.decision_binding_sha256, d1_repeat.decision_binding_sha256)
        self.assertNotEqual(d1.decision_binding_sha256, d2.decision_binding_sha256)
        self.assertEqual(len(d1.decision_binding_sha256), 64)


def _candidate(**overrides: object) -> TenantMergeCandidate:
    payload = {
        "product": PRODUCT,
        "context": CONTEXT,
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
    kind: str = "tenant_ui",
    revision: int = 1,
    **overrides: object,
) -> TenantRepositoryClassificationRecord:
    payload = {
        "product": PRODUCT,
        "context": CONTEXT,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "classification_kind": kind,
        "classification_revision": revision,
        "classified_at": CLASSIFIED_AT,
        "source": SOURCE,
        "reason": REASON,
    }
    payload.update(overrides)
    return TenantRepositoryClassificationRecord.model_validate(payload)


def _lookup(*records: TenantRepositoryClassificationRecord) -> TenantRepositoryClassificationLookup:
    return TenantRepositoryClassificationLookup(records=records)


if __name__ == "__main__":
    unittest.main()
