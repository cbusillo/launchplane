from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathResult,
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.manager_preview_approval import record_manager_preview_approval_event
from control_plane.repository_human_admission import (
    capture_tenant_technical_human_waiver_event,
)
from control_plane.service_auth import GitHubHumanIdentity
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_admission_status import (
    TenantAdmissionStatusReadModel,
    TenantAdmissionStatusStore,
    get_tenant_admission_status,
)
from control_plane.trusted_maintenance import capture_trusted_maintenance_evidence
from tests.test_manager_preview_approval import (
    CONTEXT as MANAGER_CONTEXT,
    HEAD_SHA as MANAGER_HEAD_SHA,
    PRODUCT as MANAGER_PRODUCT,
    REPOSITORY as MANAGER_REPOSITORY,
    _generation as manager_generation,
    _manager_identity,
    _policy_record as manager_policy_record,
    _preview as manager_preview,
    _role_policy as manager_role_policy,
)
from tests.test_repository_human_admission import (
    EVALUATED_AT as WAIVER_EVALUATED_AT,
    OCCURRED_AT as WAIVER_OCCURRED_AT,
    _authz_policy_record as waiver_authz_policy_record,
    _candidate as waiver_candidate,
    _classification as waiver_classification,
    _role_policy as waiver_role_policy,
    _write_authz_policy_record,
)
from tests.test_trusted_maintenance import (
    OCCURRED_AT as MAINTENANCE_OCCURRED_AT,
    _candidate as maintenance_candidate,
    _classification as maintenance_classification,
    _event_facts as maintenance_event_facts,
    _policy as maintenance_policy,
)


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
PULL_REQUEST_NUMBER = 17
EVALUATED_AT = "2026-07-31T12:10:00Z"


class TenantAdmissionStatusTests(unittest.TestCase):
    def test_engineering_repository_skips_tenant_evidence_reads(self) -> None:
        candidate = _candidate()
        store = _classification_only_store((_classification(kind="engineering"),))
        with (
            patch(
                "control_plane.tenant_admission_status._trusted_maintenance_path",
                side_effect=AssertionError("engineering path must not read tenant evidence"),
            ),
            patch(
                "control_plane.tenant_admission_status._technical_human_waiver_path",
                side_effect=AssertionError("engineering path must not read tenant evidence"),
            ),
            patch(
                "control_plane.tenant_admission_status._manager_preview_approval_path",
                side_effect=AssertionError("engineering path must not read tenant evidence"),
            ),
        ):
            status = get_tenant_admission_status(
                store=store,
                candidate=candidate,
                evaluated_at=EVALUATED_AT,
            )

        self.assertEqual(status.category, "engineering")
        self.assertTrue(status.decision.admitted)
        self.assertEqual(status.paths.model_dump(exclude_none=True), {"schema_version": 1})

    def test_each_tenant_path_is_independently_sufficient(self) -> None:
        cases = (
            ("trusted_maintenance", "maintenance-admitted"),
            ("technical_human_waiver", "technical-waived"),
            ("manager_preview_approval", "manager-approved"),
        )
        for satisfied_kind, expected_category in cases:
            with self.subTest(path=satisfied_kind):
                status = _status_with_path_states(
                    trusted_state=(
                        "satisfied" if satisfied_kind == "trusted_maintenance" else "pending"
                    ),
                    waiver_state=(
                        "satisfied" if satisfied_kind == "technical_human_waiver" else "pending"
                    ),
                    manager_state=(
                        "satisfied" if satisfied_kind == "manager_preview_approval" else "pending"
                    ),
                )

                self.assertEqual(status.category, expected_category)
                self.assertTrue(status.decision.admitted)
                self.assertEqual(status.decision.evidence_kind, satisfied_kind)

    def test_multiple_satisfied_paths_preserve_existing_precedence(self) -> None:
        status = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="satisfied",
            manager_state="satisfied",
        )

        self.assertEqual(status.category, "maintenance-admitted")
        self.assertEqual(status.decision.evidence_kind, "trusted_maintenance")

    def test_manager_blocking_states_map_to_public_categories(self) -> None:
        for manager_state, expected_category in (
            ("pending", "pending"),
            ("denied", "denied"),
            ("stale", "stale"),
            ("unavailable", "unavailable"),
        ):
            with self.subTest(state=manager_state):
                status = _status_with_path_states(
                    trusted_state="pending",
                    waiver_state="pending",
                    manager_state=manager_state,
                )
                self.assertEqual(status.category, expected_category)
                self.assertFalse(status.decision.admitted)

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

    def test_real_manager_preview_approval_admits_exact_head(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            candidate = _candidate(
                product=MANAGER_PRODUCT,
                context=MANAGER_CONTEXT,
                repository=MANAGER_REPOSITORY,
                head_sha=MANAGER_HEAD_SHA,
            )
            classification = _classification(
                product=MANAGER_PRODUCT,
                context=MANAGER_CONTEXT,
                repository=MANAGER_REPOSITORY,
            )
            policy = manager_policy_record()
            store.write_tenant_repository_classification_record(classification)
            _write_authz_policy_record(store, policy)
            store.write_preview_record(manager_preview())
            store.write_preview_generation_record(manager_generation())
            record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=policy,
                product=MANAGER_PRODUCT,
                preview=manager_preview(),
                generation=manager_generation(),
                action="approved",
                occurred_at="2026-07-30T12:00:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-tenant-admission",
            )

            status = get_tenant_admission_status(
                store=store,
                candidate=candidate,
                evaluated_at="2026-07-30T12:10:00Z",
            )

        self.assertEqual(status.category, "manager-approved")
        self.assertEqual(
            _required_path(status.paths.manager_preview_approval).state,
            "satisfied",
        )
        serialized = json.dumps(status.model_dump(mode="json"), sort_keys=True)
        self.assertNotIn("manager_github_id", serialized)
        self.assertNotIn("manager_login", serialized)
        self.assertNotIn("policy_source", serialized)

    def test_active_role_policy_rejects_legacy_manager_approval(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            candidate = _candidate(
                product=MANAGER_PRODUCT,
                context=MANAGER_CONTEXT,
                repository=MANAGER_REPOSITORY,
                head_sha=MANAGER_HEAD_SHA,
            )
            classification = _classification(
                product=MANAGER_PRODUCT,
                context=MANAGER_CONTEXT,
                repository=MANAGER_REPOSITORY,
            )
            policy = manager_policy_record()
            store.write_tenant_repository_classification_record(classification)
            store.write_repository_human_role_policy_record(
                manager_role_policy(manager_primary_ids=(101,))
            )
            _write_authz_policy_record(store, policy)
            store.write_preview_record(manager_preview())
            store.write_preview_generation_record(manager_generation())
            record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=policy,
                product=MANAGER_PRODUCT,
                preview=manager_preview(),
                generation=manager_generation(),
                action="approved",
                occurred_at="2026-07-30T12:00:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-legacy-role-policy",
            )

            status = get_tenant_admission_status(
                store=store,
                candidate=candidate,
                evaluated_at="2026-07-30T12:10:00Z",
            )

        self.assertFalse(status.decision.admitted)
        self.assertEqual(status.category, "stale")
        self.assertEqual(_required_path(status.paths.manager_preview_approval).state, "stale")

    def test_real_manager_preview_head_drift_is_stale(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            candidate = _candidate(
                product=MANAGER_PRODUCT,
                context=MANAGER_CONTEXT,
                repository=MANAGER_REPOSITORY,
                head_sha="2" * 40,
            )
            classification = _classification(
                product=MANAGER_PRODUCT,
                context=MANAGER_CONTEXT,
                repository=MANAGER_REPOSITORY,
            )
            policy = manager_policy_record()
            store.write_tenant_repository_classification_record(classification)
            _write_authz_policy_record(store, policy)
            store.write_preview_record(manager_preview())
            store.write_preview_generation_record(manager_generation())
            record_manager_preview_approval_event(
                record_store=store,
                identity=_manager_identity(),
                policy_record=policy,
                product=MANAGER_PRODUCT,
                preview=manager_preview(),
                generation=manager_generation(),
                action="approved",
                occurred_at="2026-07-30T12:00:00Z",
                source_event_kind="github_issue_comment",
                source_event_id="comment-stale-head",
            )

            status = get_tenant_admission_status(
                store=store,
                candidate=candidate,
                evaluated_at="2026-07-30T12:10:00Z",
            )

        self.assertEqual(status.category, "stale")
        self.assertEqual(_required_path(status.paths.manager_preview_approval).state, "stale")

    def test_legacy_short_repository_name_requires_canonical_pull_request_url(self) -> None:
        legacy_repository = MANAGER_REPOSITORY.split("/", 1)[1]
        invalid_urls = (
            f"https://github.com/other-owner/{legacy_repository}/pull/{PULL_REQUEST_NUMBER}",
            f"https://example.test/{MANAGER_REPOSITORY}/pull/{PULL_REQUEST_NUMBER}",
        )
        for index, invalid_url in enumerate(invalid_urls, start=1):
            with self.subTest(pull_request_url=invalid_url), TemporaryDirectory() as temporary_name:
                store = FilesystemRecordStore(state_dir=Path(temporary_name))
                candidate = _candidate(
                    product=MANAGER_PRODUCT,
                    context=MANAGER_CONTEXT,
                    repository=MANAGER_REPOSITORY,
                    head_sha=MANAGER_HEAD_SHA,
                )
                classification = _classification(
                    product=MANAGER_PRODUCT,
                    context=MANAGER_CONTEXT,
                    repository=MANAGER_REPOSITORY,
                )
                policy = manager_policy_record()
                invalid_preview = manager_preview(
                    anchor_repo=legacy_repository,
                    anchor_pr_url=invalid_url,
                )
                invalid_generation = manager_generation(
                    anchor_summary=manager_generation().anchor_summary.model_copy(
                        update={
                            "repo": legacy_repository,
                            "pr_url": invalid_url,
                        }
                    )
                )
                store.write_tenant_repository_classification_record(classification)
                _write_authz_policy_record(store, policy)
                store.write_preview_record(invalid_preview)
                store.write_preview_generation_record(invalid_generation)
                record_manager_preview_approval_event(
                    record_store=store,
                    identity=_manager_identity(),
                    policy_record=policy,
                    product=MANAGER_PRODUCT,
                    preview=invalid_preview,
                    generation=invalid_generation,
                    action="approved",
                    occurred_at="2026-07-30T12:00:00Z",
                    source_event_kind="github_issue_comment",
                    source_event_id=f"comment-invalid-url-{index}",
                )

                status = get_tenant_admission_status(
                    store=store,
                    candidate=candidate,
                    evaluated_at="2026-07-30T12:10:00Z",
                )

            self.assertFalse(status.decision.admitted)
            self.assertNotEqual(status.category, "manager-approved")
            self.assertEqual(
                _required_path(status.paths.manager_preview_approval).state,
                "unavailable",
            )

    def test_real_technical_human_waiver_admits_exact_head(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            candidate = waiver_candidate()
            classification = waiver_classification()
            role_policy = waiver_role_policy(repository_owner_ids=(301,))
            authz_policy = waiver_authz_policy_record(github_ids=(301,))
            event = capture_tenant_technical_human_waiver_event(
                identity=GitHubHumanIdentity(
                    login="repository-owner",
                    github_id=301,
                    name="Repository Owner",
                    email="",
                    organizations=frozenset(),
                    teams=frozenset(),
                    role="read_only",
                ),
                candidate=candidate,
                classification=classification,
                role_policy_record=role_policy,
                authz_policy_record=authz_policy,
                action="created",
                occurred_at=WAIVER_OCCURRED_AT,
                recorded_at=WAIVER_OCCURRED_AT,
                source_event_kind="github_issue_comment",
                source_event_id="comment-waiver-status",
                reason="Owner approved exact technical handling.",
            ).record
            store.write_tenant_repository_classification_record(classification)
            store.write_repository_human_role_policy_record(role_policy)
            _write_authz_policy_record(store, authz_policy)
            store.write_tenant_technical_human_waiver_event_record(event)

            status = get_tenant_admission_status(
                store=store,
                candidate=candidate,
                evaluated_at=WAIVER_EVALUATED_AT,
            )

        self.assertEqual(status.category, "technical-waived")
        self.assertEqual(_required_path(status.paths.technical_human_waiver).state, "satisfied")

    def test_real_trusted_maintenance_admits_exact_head(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            candidate = maintenance_candidate()
            classification = maintenance_classification()
            policy = maintenance_policy()
            evidence = capture_trusted_maintenance_evidence(
                candidate=candidate,
                classification=classification,
                policy_record=policy,
                event_facts=maintenance_event_facts(),
                occurred_at=MAINTENANCE_OCCURRED_AT,
                recorded_at=MAINTENANCE_OCCURRED_AT,
            ).record
            store.write_tenant_repository_classification_record(classification)
            store.write_trusted_maintenance_policy_record(policy)
            store.write_trusted_maintenance_evidence_record(evidence)

            status = get_tenant_admission_status(
                store=store,
                candidate=candidate,
                evaluated_at="2026-07-31T10:20:00Z",
            )

        self.assertEqual(status.category, "maintenance-admitted")
        self.assertEqual(_required_path(status.paths.trusted_maintenance).state, "satisfied")


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
        "product": MANAGER_PRODUCT,
        "context": MANAGER_CONTEXT,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": MANAGER_REPOSITORY,
        "pull_request_number": PULL_REQUEST_NUMBER,
        "head_sha": MANAGER_HEAD_SHA,
    }
    payload.update(overrides)
    return TenantMergeCandidate.model_validate(payload)


def _classification_only_store(
    records: tuple[TenantRepositoryClassificationRecord, ...],
) -> TenantAdmissionStatusStore:
    return cast(TenantAdmissionStatusStore, _ClassificationStore(records))


def _required_path(path: TenantAdmissionPathResult | None) -> TenantAdmissionPathResult:
    if path is None:
        raise AssertionError("Expected tenant admission path result.")
    return path


def _classification(
    *,
    kind: str = "tenant_ui",
    product: str = MANAGER_PRODUCT,
    context: str = MANAGER_CONTEXT,
    repository: str = MANAGER_REPOSITORY,
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


def _path_result(
    *,
    kind: str,
    state: str,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
) -> TenantAdmissionPathResult:
    payload: dict[str, object] = {
        "path_kind": kind,
        "state": state,
        "repository_id": candidate.repository_id,
        "repository_owner_id": candidate.repository_owner_id,
        "repository": candidate.repository,
        "pull_request_number": candidate.pull_request_number,
        "head_sha": candidate.head_sha,
        "classification_digest": classification.classification_digest,
    }
    if state == "satisfied":
        payload["evidence_id"] = f"{kind}-evidence"
        payload["evidence_digest"] = {
            "trusted_maintenance": "a" * 64,
            "technical_human_waiver": "b" * 64,
            "manager_preview_approval": "c" * 64,
        }[kind]
    return TenantAdmissionPathResult.model_validate(payload)


def _status_with_path_states(
    *,
    trusted_state: str,
    waiver_state: str,
    manager_state: str,
) -> TenantAdmissionStatusReadModel:
    candidate = _candidate()
    classification = _classification()
    store = _classification_only_store((classification,))
    trusted = _path_result(
        kind="trusted_maintenance",
        state=trusted_state,
        candidate=candidate,
        classification=classification,
    )
    waiver = _path_result(
        kind="technical_human_waiver",
        state=waiver_state,
        candidate=candidate,
        classification=classification,
    )
    manager = _path_result(
        kind="manager_preview_approval",
        state=manager_state,
        candidate=candidate,
        classification=classification,
    )
    with (
        patch(
            "control_plane.tenant_admission_status._trusted_maintenance_path",
            return_value=trusted,
        ),
        patch(
            "control_plane.tenant_admission_status._technical_human_waiver_path",
            return_value=waiver,
        ),
        patch(
            "control_plane.tenant_admission_status._manager_preview_approval_path",
            return_value=manager,
        ),
    ):
        return get_tenant_admission_status(
            store=store,
            candidate=candidate,
            evaluated_at=EVALUATED_AT,
        )
