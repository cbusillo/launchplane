from __future__ import annotations

from typing import cast
import unittest

from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.tenant_admission_status import (
    TenantAdmissionStatusReadModel,
    TenantAdmissionStatusStore,
    get_tenant_admission_status,
)

PRODUCT = "example-site"
CONTEXT = "example-site-testing"
REPOSITORY = "example/example-site"
HEAD_SHA = "1" * 40
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
PULL_REQUEST_NUMBER = 17
EVALUATED_AT = "2026-07-31T12:10:00Z"


class TenantAdmissionStatusTests(unittest.TestCase):
    def test_classified_repositories_need_no_retired_evidence_storage(self) -> None:
        for kind, category in (("engineering", "engineering"), ("tenant_ui", "eligible")):
            with self.subTest(kind=kind):
                status = get_tenant_admission_status(
                    store=_classification_only_store((_classification(kind=kind),)),
                    candidate=_candidate(),
                    evaluated_at=EVALUATED_AT,
                )
                self.assertEqual(status.category, category)
                self.assertTrue(status.decision.admitted)
                self.assertEqual(status.paths.model_dump(exclude_none=True), {"schema_version": 1})

    def test_missing_ambiguous_and_drifted_classification_fail_closed(self) -> None:
        candidate = _candidate()
        missing = get_tenant_admission_status(
            store=_classification_only_store(()),
            candidate=candidate,
            evaluated_at=EVALUATED_AT,
        )
        duplicate_revision = _classification(reason="duplicate highest revision")
        ambiguous = get_tenant_admission_status(
            store=_classification_only_store((_classification(), duplicate_revision)),
            candidate=candidate,
            evaluated_at=EVALUATED_AT,
        )
        drifted = get_tenant_admission_status(
            store=_classification_only_store((_classification(product="other-product"),)),
            candidate=candidate,
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(missing.category, "unavailable")
        self.assertEqual(missing.decision.reason_code, "classification_missing")
        self.assertEqual(ambiguous.category, "unavailable")
        self.assertEqual(ambiguous.decision.reason_code, "classification_ambiguous")
        self.assertEqual(drifted.category, "stale")
        self.assertEqual(drifted.decision.reason_code, "classification_identity_drift")


class _ClassificationStore:
    def __init__(self, records: tuple[TenantRepositoryClassificationRecord, ...]) -> None:
        self.records = records

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        records = tuple(
            record
            for record in self.records
            if not repository_id or record.repository_id == repository_id
        )
        return records[:limit] if limit is not None else records


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


def _classification_only_store(
    records: tuple[TenantRepositoryClassificationRecord, ...],
) -> TenantAdmissionStatusStore:
    return cast(TenantAdmissionStatusStore, _ClassificationStore(records))


def _classification(
    *,
    kind: str = "tenant_ui",
    product: str = PRODUCT,
    context: str = CONTEXT,
    repository: str = REPOSITORY,
    reason: str = "tenant admission status test",
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord.model_validate(
        {
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "repository": repository,
            "product": product,
            "context": context,
            "classification_kind": kind,
            "classification_revision": 1,
            "classified_at": "2026-07-30T11:00:00Z",
            "source": "test:tenant-admission-status",
            "reason": reason,
        }
    )


def _eligible_status() -> TenantAdmissionStatusReadModel:
    return get_tenant_admission_status(
        store=_classification_only_store((_classification(),)),
        candidate=_candidate(),
        evaluated_at=EVALUATED_AT,
    )
