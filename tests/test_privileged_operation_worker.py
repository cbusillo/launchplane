from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane import secrets as control_plane_secrets
from control_plane.cli import main
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_REVOKE_ACTION,
    ManagedAuthzPolicySetExecutionEvidence,
    ManagedAuthzPolicySetProposalInput,
    ManagedMergeTrainPolicyImportExecutionEvidence,
    ManagedMergeTrainPolicyImportProposalInput,
    ManagedSecretReencryptionPlanInput,
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
    PrivilegedOperationApproval,
    PrivilegedOperationActor,
    PrivilegedOperationEventRecord,
    PrivilegedOperationEventWriteStatus,
    PrivilegedOperationRecord,
    privileged_operation_pre_state_digest,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PrivilegedOperationWorkerHeartbeatRecord,
)
from control_plane.privileged_operation_registry import (
    MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
    RegisteredPrivilegedOperationDescriptor,
)
from control_plane.privileged_operation_service import (
    approve_privileged_operation,
    create_privileged_operation_plan,
    create_typed_privileged_operation_plan,
    PrivilegedOperationNotApprovableError,
)
from control_plane.privileged_operation_worker import (
    PRIVILEGED_OPERATION_EXECUTION_ROUTE,
    execute_approved_privileged_operations_once,
    privileged_operation_execution_fingerprint,
    privileged_operation_execution_token,
    privileged_operation_provider_target_key,
    record_privileged_operation_worker_poll_heartbeat,
    require_privileged_operation_execution_store,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record


FIXED_NOW = datetime(2026, 8, 22, 20, 10, tzinfo=timezone.utc)


class _WorkerStore:
    def __init__(self, *, fail_heartbeat: bool = False) -> None:
        self.heartbeat_records: list[PrivilegedOperationWorkerHeartbeatRecord] = []
        self.fail_heartbeat = fail_heartbeat

    def write_privileged_operation_worker_heartbeat_record(
        self,
        record: PrivilegedOperationWorkerHeartbeatRecord,
        *,
        prune_before: str,
        prune_after: str,
    ) -> None:
        del prune_before, prune_after
        if self.fail_heartbeat:
            raise RuntimeError("heartbeat write failed")
        self.heartbeat_records.append(record)


def _build_worker_store_with_successful_probe(**kwargs: object) -> object:
    report_probe_succeeded = kwargs["on_schema_probe_succeeded"]
    assert callable(report_probe_succeeded)
    report_probe_succeeded()
    return _WorkerStore()


def _fernet_key(offset: int) -> str:
    return base64.urlsafe_b64encode(bytes((offset + index) % 256 for index in range(32))).decode()


def _policy_record(
    *,
    revision: int,
    github_ids: tuple[int, ...] = (123,),
    logins: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": [
                {
                    "managed_set_id": "privileged-operations.secret-execution",
                    "managed_rule_id": "human-secret-approver",
                    "github_ids": list(github_ids),
                    "logins": list(logins),
                    "roles": list(roles),
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
                        PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
                    ],
                }
            ],
        }
    )
    return LaunchplaneAuthzPolicyRecord(
        record_id=f"launchplane-authz-policy-r{revision}",
        revision=revision,
        source=f"test-policy-r{revision}",
        updated_at=f"2026-08-22T19:{revision:02d}:00+00:00",
        policy=policy,
    )


def _prepare_approved_operation(
    store: PostgresRecordStore,
    *,
    approval_policy: LaunchplaneAuthzPolicyRecord,
) -> tuple[str, str]:
    old_key = _fernet_key(0)
    new_key = _fernet_key(32)
    with patch.dict(
        os.environ,
        {
            control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                {"active_key_id": "key-1", "keys": {"key-1": old_key}}
            )
        },
        clear=True,
    ):
        written = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="global",
            integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
            name="host",
            plaintext_value="https://provider.example/private",
            binding_key="DOKPLOY_HOST",
            actor="test",
        )
    with patch.dict(
        os.environ,
        {
            control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                {
                    "active_key_id": "key-2",
                    "keys": {"key-1": old_key, "key-2": new_key},
                }
            )
        },
        clear=True,
    ):
        planned = create_privileged_operation_plan(
            record_store=store,
            requester_github_id=123,
            requester_login="operator-at-approval",
            source_event_id="worker-test-plan",
            request=ManagedSecretReencryptionPlanInput(
                reason="Rotate managed secrets under the canonical root."
            ),
            now=lambda: datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc),
        ).record
    approval = PrivilegedOperationApproval(
        approver=PrivilegedOperationActor(
            identity_type="github_human",
            github_id=123,
            login="operator-at-approval",
        ),
        descriptor_id=planned.descriptor_id,
        descriptor_version=planned.descriptor_version,
        request_digest=planned.request_digest,
        evidence_digest=planned.evidence_digest,
        plan_digest=planned.evidence.plan_digest,
        pre_state_digest=privileged_operation_pre_state_digest(planned.evidence),
        policy_record_id=approval_policy.record_id,
        policy_revision=approval_policy.revision,
        policy_sha256=approval_policy.policy_sha256,
        policy_source=approval_policy.source,
        managed_set_id="privileged-operations.secret-execution",
        managed_rule_id="human-secret-approver",
        expires_at=planned.expires_at,
        reason="Reviewed the redacted plan evidence.",
    )
    approved = approve_privileged_operation(
        record_store=store,
        operation_id=planned.operation_id,
        approval=approval,
        source_event_id="worker-test-approval",
        now=lambda: datetime(2026, 8, 22, 20, 5, tzinfo=timezone.utc),
    ).record
    return approved.operation_id, str(written["secret_id"])


def _policy_admin_record(
    *,
    revision: int = 1,
    administrator_quorum: int | None = None,
    approver_is_policy_admin: bool = True,
    approver_organizations: tuple[str, ...] = (),
    include_same_approver_scoped_admin: bool = False,
) -> LaunchplaneAuthzPolicyRecord:
    approver_actions = [
        AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
        AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
    ]
    if approver_is_policy_admin:
        approver_actions.append("authz_policy_grant.write")
    github_humans: list[dict[str, object]] = [
        {
            "managed_set_id": "privileged-operations.policy-execution",
            "managed_rule_id": "human-policy-approver",
            "github_ids": [123],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": approver_actions,
            "organizations": list(approver_organizations),
        },
        {
            "managed_set_id": "privileged-operations.policy-safety",
            "managed_rule_id": "independent-policy-admin",
            "github_ids": [456],
            "roles": ["admin"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            "actions": ["authz_policy_grant.write"],
        },
    ]
    if include_same_approver_scoped_admin:
        github_humans.append(
            {
                "managed_set_id": "privileged-operations.policy-shadow",
                "managed_rule_id": "same-approver-org-admin",
                "github_ids": [123],
                "organizations": ["solo-org"],
                "roles": ["admin"],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": ["authz_policy_grant.write"],
            }
        )
    policy = LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "administrator_quorum": administrator_quorum,
            "github_humans": github_humans,
        }
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=revision,
            policy_sha256=policy_sha256,
        ),
        revision=revision,
        source=f"test-policy-admin-r{revision}",
        updated_at=f"2026-08-22T19:{revision:02d}:00+00:00",
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _merge_train_policy_admin_record(*, revision: int = 1) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": [
                {
                    "managed_set_id": "privileged-operations.merge-train-policy-execution",
                    "managed_rule_id": "human-merge-train-policy-approver",
                    "github_ids": [123],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        MERGE_TRAIN_POLICY_OPERATION_APPROVE_ACTION,
                        MERGE_TRAIN_POLICY_OPERATION_REVOKE_ACTION,
                    ],
                }
            ],
        }
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=revision,
            policy_sha256=policy_sha256,
        ),
        revision=revision,
        source=f"test-merge-train-policy-admin-r{revision}",
        updated_at=f"2026-08-22T19:{revision:02d}:00+00:00",
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _prepare_approved_policy_operation(
    store: PostgresRecordStore,
    *,
    approval_policy: LaunchplaneAuthzPolicyRecord,
    request: ManagedAuthzPolicySetProposalInput | None = None,
) -> str:
    resolved_request = request or ManagedAuthzPolicySetProposalInput(
        managed_set_id="test.policy-operation",
        reason="Add a reviewed policy-operation read rule.",
        related_issue="cbusillo/launchplane#2238",
        desired_policy=LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "managed_set_id": "test.policy-operation",
                        "managed_rule_id": "policy-operation-reader",
                        "github_ids": [789],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_operation.read"],
                    }
                ],
            }
        ),
    )
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=123,
        login="operator-at-approval",
    )
    planned = create_typed_privileged_operation_plan(
        record_store=store,
        descriptor_id="managed-authz-policy-set",
        actor=actor,
        source_kind="browser_api",
        source_event_id="worker-policy-plan",
        request=resolved_request,
        now=lambda: datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc),
    ).record
    approval = PrivilegedOperationApproval(
        approver=actor,
        descriptor_id=planned.descriptor_id,
        descriptor_version=planned.descriptor_version,
        request_digest=planned.request_digest,
        evidence_digest=planned.evidence_digest,
        plan_digest=planned.evidence.plan_digest,
        pre_state_digest=privileged_operation_pre_state_digest(planned.evidence),
        policy_record_id=approval_policy.record_id,
        policy_revision=approval_policy.revision,
        policy_sha256=approval_policy.policy_sha256,
        policy_source=approval_policy.source,
        managed_set_id="privileged-operations.policy-execution",
        managed_rule_id="human-policy-approver",
        expires_at=planned.expires_at,
        reason="Reviewed the exact policy diff and immutable policy state.",
        rollback_class="policy_cas",
    )
    return approve_privileged_operation(
        record_store=store,
        operation_id=planned.operation_id,
        approval=approval,
        source_event_id="worker-policy-approval",
        now=lambda: datetime(2026, 8, 22, 20, 5, tzinfo=timezone.utc),
    ).record.operation_id


def _prepare_approved_merge_train_policy_import(
    store: PostgresRecordStore,
    *,
    approval_policy: LaunchplaneAuthzPolicyRecord,
    active_record: MergeTrainPolicyRecord | None = None,
    candidate_record: MergeTrainPolicyRecord | None = None,
) -> str:
    resolved_active = active_record or build_test_merge_train_policy_record(
        repository="cbusillo/sellyouroutboard",
        record_id="merge-train-policy-active",
        updated_at="2026-08-22T19:00:00Z",
    )
    resolved_candidate = candidate_record or build_test_merge_train_policy_record(
        repository="cbusillo/codex-skills",
        record_id="merge-train-policy-candidate",
        updated_at="2026-08-22T20:00:00Z",
    )
    store.write_merge_train_policy_record(resolved_active)
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=123,
        login="operator-at-approval",
    )
    planned = create_typed_privileged_operation_plan(
        record_store=store,
        descriptor_id="managed-merge-train-policy-import",
        actor=actor,
        source_kind="browser_api",
        source_event_id="worker-merge-train-policy-plan",
        request=ManagedMergeTrainPolicyImportProposalInput(
            record=resolved_candidate,
            reason="Review exact merge-train policy import.",
            related_issue="cbusillo/launchplane#2296",
        ),
        now=lambda: datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc),
    ).record
    approval = PrivilegedOperationApproval(
        approver=actor,
        descriptor_id=planned.descriptor_id,
        descriptor_version=planned.descriptor_version,
        request_digest=planned.request_digest,
        evidence_digest=planned.evidence_digest,
        plan_digest=planned.evidence.plan_digest,
        pre_state_digest=privileged_operation_pre_state_digest(planned.evidence),
        policy_record_id=approval_policy.record_id,
        policy_revision=approval_policy.revision,
        policy_sha256=approval_policy.policy_sha256,
        policy_source=approval_policy.source,
        managed_set_id="privileged-operations.merge-train-policy-execution",
        managed_rule_id="human-merge-train-policy-approver",
        expires_at=planned.expires_at,
        reason="Reviewed exact merge-train policy import evidence.",
        rollback_class="policy_cas",
    )
    return approve_privileged_operation(
        record_store=store,
        operation_id=planned.operation_id,
        approval=approval,
        source_event_id="worker-merge-train-policy-approval",
        now=lambda: datetime(2026, 8, 22, 20, 5, tzinfo=timezone.utc),
    ).record.operation_id


class PrivilegedOperationWorkerTests(unittest.TestCase):
    def _store(self, directory: str) -> PostgresRecordStore:
        store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
        )
        store.ensure_schema()
        return store

    def _execution_environment(self) -> dict[str, str]:
        return {
            control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR: json.dumps(
                {
                    "active_key_id": "key-2",
                    "keys": {"key-1": _fernet_key(0), "key-2": _fernet_key(32)},
                }
            )
        }

    def test_execution_requires_postgres_mutation_reservations(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TypeError, "PostgreSQL mutation storage"):
                require_privileged_operation_execution_store(FilesystemRecordStore(Path(directory)))

    def test_worker_executes_approved_managed_authz_policy_set_with_cas_readback(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _policy_admin_record(include_same_approver_scoped_admin=True)
                )
                operation_id = _prepare_approved_policy_operation(
                    store,
                    approval_policy=approval_policy,
                )

                completed = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual([item.operation_id for item in completed], [operation_id])
        self.assertEqual(record.status, "executed")
        self.assertIsInstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        assert isinstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        self.assertTrue(record.execution.changed)
        self.assertEqual(record.execution.previous_revision, 1)
        self.assertEqual(record.execution.resulting_revision, 2)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 2)
        self.assertTrue(
            any(
                rule.managed_rule_id == "policy-operation-reader"
                for rule in active_records[0].policy.github_humans
            )
        )

    def test_worker_rejects_org_scoped_approver_as_continuity_admin(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _policy_admin_record(
                        approver_organizations=("solo-org",), administrator_quorum=1
                    )
                )
                operation_id = _prepare_approved_policy_operation(
                    store,
                    approval_policy=approval_policy,
                )

                execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(record.status, "execution_failed")
        self.assertIsInstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        assert isinstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        self.assertEqual(record.execution.failure_code, "authz_policy_applying_admin_removed")
        self.assertFalse(record.execution.reconciliation_required)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 1)

    def test_worker_executes_approved_merge_train_policy_import_with_cas_readback(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _merge_train_policy_admin_record()
                )
                operation_id = _prepare_approved_merge_train_policy_import(
                    store,
                    approval_policy=approval_policy,
                )

                completed = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_merge_train_policy_records(status="active", limit=2)
                superseded_records = store.list_merge_train_policy_records(
                    status="superseded",
                    limit=10,
                )
            finally:
                store.close()

        self.assertEqual([item.operation_id for item in completed], [operation_id])
        self.assertEqual(record.status, "executed")
        self.assertIsInstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        assert isinstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        self.assertTrue(record.execution.changed)
        self.assertEqual(record.execution.previous_record_id, "merge-train-policy-active")
        self.assertEqual(record.execution.resulting_record_id, "merge-train-policy-candidate")
        self.assertEqual(record.execution.superseded_record_id, "merge-train-policy-active")
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].record_id, "merge-train-policy-candidate")
        self.assertEqual(
            [item.record_id for item in superseded_records], ["merge-train-policy-active"]
        )

    def test_worker_treats_same_policy_with_new_record_id_as_unchanged(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _merge_train_policy_admin_record()
                )
                active_record = build_test_merge_train_policy_record(
                    repository="cbusillo/sellyouroutboard",
                    record_id="merge-train-policy-active",
                    updated_at="2026-09-02T00:00:00Z",
                )
                candidate_record = build_test_merge_train_policy_record(
                    repository="cbusillo/sellyouroutboard",
                    record_id="merge-train-policy-candidate",
                    updated_at="2026-09-02T00:01:00Z",
                )
                operation_id = _prepare_approved_merge_train_policy_import(
                    store,
                    approval_policy=approval_policy,
                    active_record=active_record,
                    candidate_record=candidate_record,
                )

                completed = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_merge_train_policy_records(status="active", limit=2)
                superseded_records = store.list_merge_train_policy_records(
                    status="superseded",
                    limit=10,
                )
            finally:
                store.close()

        self.assertEqual([item.operation_id for item in completed], [operation_id])
        self.assertEqual(record.status, "executed")
        self.assertIsInstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        assert isinstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        self.assertFalse(record.execution.changed)
        self.assertEqual(record.execution.resulting_record_id, active_record.record_id)
        self.assertEqual(active_records, (active_record,))
        self.assertEqual(superseded_records, ())

    def test_worker_rejects_merge_train_policy_import_when_active_policy_drifts(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _merge_train_policy_admin_record()
                )
                operation_id = _prepare_approved_merge_train_policy_import(
                    store,
                    approval_policy=approval_policy,
                )
                active_record = store.list_merge_train_policy_records(status="active", limit=1)[0]
                drift_record = build_test_merge_train_policy_record(
                    repository="cbusillo/odoo-devkit",
                    record_id="merge-train-policy-drift",
                    updated_at="2026-08-22T20:06:00+00:00",
                )
                store.compare_and_write_merge_train_policy_record(
                    expected_record=active_record,
                    replacement_record=drift_record,
                )

                execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_merge_train_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(record.status, "execution_failed")
        self.assertIsInstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        assert isinstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        self.assertEqual(record.execution.failure_code, "approved_plan_drift")
        self.assertFalse(record.execution.reconciliation_required)
        self.assertEqual(active_records[0].record_id, "merge-train-policy-drift")

    def test_worker_reads_exact_superseded_policy_beyond_history_window(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _merge_train_policy_admin_record()
                )
                operation_id = _prepare_approved_merge_train_policy_import(
                    store,
                    approval_policy=approval_policy,
                )
                for index in range(101):
                    store.write_merge_train_policy_record(
                        build_test_merge_train_policy_record(
                            repository=f"cbusillo/history-{index}",
                            record_id=f"merge-train-policy-history-{index:03d}",
                            updated_at=f"2026-09-02T01:{index // 60:02d}:{index % 60:02d}Z",
                        ).model_copy(update={"status": "superseded"})
                    )

                completed = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
            finally:
                store.close()

        self.assertEqual([item.operation_id for item in completed], [operation_id])
        self.assertEqual(record.status, "executed")
        self.assertIsInstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        assert isinstance(record.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        self.assertEqual(record.execution.superseded_record_id, "merge-train-policy-active")

    def test_policy_readback_failure_after_cas_requires_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(_policy_admin_record())
                operation_id = _prepare_approved_policy_operation(
                    store,
                    approval_policy=approval_policy,
                )
                original_compare = store.compare_and_write_authz_policy_record
                original_list = store.list_authz_policy_records
                effect_committed = False

                def compare_without_readback(
                    *,
                    expected_record: LaunchplaneAuthzPolicyRecord,
                    replacement_record: LaunchplaneAuthzPolicyRecord | None,
                    mutation: DbOnlyMutationRequest | None = None,
                ) -> AuthzPolicyCompareWriteResult:
                    nonlocal effect_committed
                    result = original_compare(
                        expected_record=expected_record,
                        replacement_record=replacement_record,
                        mutation=mutation,
                    )
                    effect_committed = result.status == "written"
                    return AuthzPolicyCompareWriteResult(
                        status=result.status,
                        current_record=None,
                        idempotency_record=result.idempotency_record,
                    )

                def fail_post_commit_readback(
                    *,
                    status: str = "",
                    limit: int | None = None,
                ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
                    if effect_committed and status == "active":
                        return ()
                    return original_list(status=status, limit=limit)

                with (
                    patch.object(
                        store,
                        "compare_and_write_authz_policy_record",
                        side_effect=compare_without_readback,
                    ),
                    patch.object(
                        store,
                        "list_authz_policy_records",
                        side_effect=fail_post_commit_readback,
                    ),
                ):
                    execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )

                record = store.read_privileged_operation_record(operation_id)
                active_records = original_list(status="active", limit=2)
                outer_lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=privileged_operation_execution_fingerprint(record),
                )
            finally:
                store.close()

        self.assertEqual(record.status, "execution_failed")
        self.assertIsInstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        assert isinstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        self.assertEqual(record.execution.failure_code, "authz_policy_readback_failed")
        self.assertTrue(record.execution.reconciliation_required)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 2)
        self.assertIsNotNone(outer_lookup.record)
        assert outer_lookup.record is not None
        self.assertEqual(outer_lookup.record.state, "reconcile_required")

    def test_worker_rejects_policy_set_that_fails_administrator_quorum(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _policy_admin_record(include_same_approver_scoped_admin=True)
                )
                with self.assertRaises(PrivilegedOperationNotApprovableError):
                    _prepare_approved_policy_operation(
                        store,
                        approval_policy=approval_policy,
                        request=ManagedAuthzPolicySetProposalInput(
                            managed_set_id="privileged-operations.policy-safety",
                            reason="Attempt to remove the independent policy administrator.",
                            desired_policy=LaunchplaneAuthzPolicy(schema_version=2),
                        ),
                    )
            finally:
                store.close()

    def test_worker_rejects_approver_without_preexisting_policy_admin_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _policy_admin_record(approver_is_policy_admin=False, administrator_quorum=1)
                )
                operation_id = _prepare_approved_policy_operation(
                    store,
                    approval_policy=approval_policy,
                )

                execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(record.status, "execution_failed")
        self.assertIsInstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        assert isinstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        self.assertEqual(record.execution.failure_code, "approval_policy_admin_drift")
        self.assertFalse(record.execution.reconciliation_required)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 1)

    def test_worker_rejects_managed_authz_policy_pre_state_drift_before_apply(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(_policy_admin_record())
                operation_id = _prepare_approved_policy_operation(
                    store,
                    approval_policy=approval_policy,
                )
                changed_policy_payload = approval_policy.policy.model_dump(mode="json")
                changed_policy_payload["github_humans"].append(
                    {
                        "managed_set_id": "test.concurrent-policy-change",
                        "managed_rule_id": "concurrent-policy-reader",
                        "github_ids": [999],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_operation.read"],
                    }
                )
                changed_policy = LaunchplaneAuthzPolicy.model_validate(changed_policy_payload)
                changed_policy_sha256 = authz_policy_sha256(changed_policy)
                replacement = LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(
                        revision=2,
                        policy_sha256=changed_policy_sha256,
                    ),
                    revision=2,
                    status="active",
                    source="test:concurrent-policy-change",
                    updated_at="2026-08-22T20:07:00+00:00",
                    policy_sha256=changed_policy_sha256,
                    policy=changed_policy,
                )
                write_result = store.compare_and_write_authz_policy_record(
                    expected_record=approval_policy,
                    replacement_record=replacement,
                )

                execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                record = store.read_privileged_operation_record(operation_id)
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(write_result.status, "written")
        self.assertEqual(record.status, "execution_failed")
        self.assertIsInstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        assert isinstance(record.execution, ManagedAuthzPolicySetExecutionEvidence)
        self.assertEqual(record.execution.failure_code, "approved_plan_drift")
        self.assertFalse(record.execution.reconciliation_required)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].record_id, replacement.record_id)
        self.assertFalse(
            any(
                rule.managed_rule_id == "policy-operation-reader"
                for rule in active_records[0].policy.github_humans
            )
        )

    def test_successful_poll_heartbeat_is_hashed_upserted_and_pruned(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                stale_record = PrivilegedOperationWorkerHeartbeatRecord(
                    worker_identity_sha256="a" * 64,
                    image_reference=f"example@sha256:{'c' * 64}",
                    poll_interval_seconds=15,
                    last_poll_succeeded_at="2026-08-14T20:00:15+00:00",
                )
                store.write_privileged_operation_worker_heartbeat_record(
                    stale_record,
                    prune_before="2026-08-01T00:00:00+00:00",
                    prune_after="2026-09-01T00:00:00+00:00",
                )
                future_record = stale_record.model_copy(
                    update={
                        "worker_identity_sha256": "d" * 64,
                        "last_poll_succeeded_at": "2030-08-24T20:00:15+00:00",
                    }
                )
                store.write_privileged_operation_worker_heartbeat_record(
                    future_record,
                    prune_before="2026-08-01T00:00:00+00:00",
                    prune_after="2031-09-01T00:00:00+00:00",
                )

                first = record_privileged_operation_worker_poll_heartbeat(
                    record_store=store,
                    runtime_identity="worker-container-hostname",
                    image_reference=f"example@sha256:{'c' * 64}",
                    poll_interval_seconds=15,
                    now=lambda: FIXED_NOW,
                )
                second = record_privileged_operation_worker_poll_heartbeat(
                    record_store=store,
                    runtime_identity="worker-container-hostname",
                    image_reference=f"example@sha256:{'c' * 64}",
                    poll_interval_seconds=15,
                    now=lambda: FIXED_NOW,
                )

                records = store.list_privileged_operation_worker_heartbeat_records()
            finally:
                store.close()

        self.assertEqual(first.worker_identity_sha256, second.worker_identity_sha256)
        self.assertEqual(len(records), 1)
        self.assertNotIn("worker-container-hostname", str(records[0].model_dump()))

    def test_worker_commands_are_service_internal(self) -> None:
        runner = CliRunner()

        service_help = runner.invoke(main, ["service", "privileged-operation-workers", "--help"])
        run_help = runner.invoke(main, ["service", "privileged-operation-workers", "run", "--help"])
        direct_run = runner.invoke(
            main,
            [
                "service",
                "privileged-operation-workers",
                "run",
                "--database-url",
                "sqlite+pysqlite:///:memory:",
            ],
        )
        top_level = runner.invoke(main, ["privileged-operations", "--help"])

        self.assertEqual(service_help.exit_code, 0)
        self.assertEqual(run_help.exit_code, 0)
        self.assertIn("run-once", service_help.output)
        self.assertIn("run", service_help.output)
        self.assertIn("--error-backoff-seconds", run_help.output)
        self.assertIn("--max-consecutive-errors", run_help.output)
        self.assertNotIn("--operation-id", run_help.output)
        self.assertNotIn("--plan-digest", run_help.output)
        self.assertNotIn("--execute-payload", run_help.output)
        self.assertNotEqual(direct_run.exit_code, 0)
        self.assertIn("--schema-probe-fd", direct_run.output)
        self.assertNotEqual(top_level.exit_code, 0)

    def test_worker_loop_retries_redacts_and_exits_at_threshold(self) -> None:
        class TestStopEvent:
            def is_set(self) -> bool:
                return False

            def wait(self, timeout: int) -> bool:
                wait_durations.append(timeout)
                return False

        wait_durations: list[int] = []
        store_attempts = 0

        def build_store(**kwargs: object) -> object:
            nonlocal store_attempts
            store_attempts += 1
            if store_attempts == 1:
                raise RuntimeError("database-url-secret must not be emitted")
            return _build_worker_store_with_successful_probe(**kwargs)

        runner = CliRunner()
        records = [SimpleNamespace(operation_id="operation-secret-id", status="executed")]
        with (
            patch("control_plane.cli_service.Event", return_value=TestStopEvent()),
            patch(
                "control_plane.cli_service.build_privileged_operation_worker_store",
                side_effect=build_store,
            ),
            patch(
                "control_plane.cli_service.execute_approved_privileged_operations_once",
                side_effect=[
                    records,
                    RuntimeError("plan-digest must not be emitted"),
                    RuntimeError("execute-payload must not be emitted"),
                ],
            ),
            patch(
                "control_plane.cli_service._consume_privileged_operation_worker_schema_probe_evidence"
            ),
            patch("control_plane.cli_service.signal.signal", return_value=object()),
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "privileged-operation-workers",
                    "run",
                    "--database-url",
                    "sqlite+pysqlite:///:memory:",
                    "--schema-probe-fd",
                    "3",
                    "--poll-seconds",
                    "7",
                    "--limit",
                    "4",
                    "--error-backoff-seconds",
                    "3",
                    "--max-consecutive-errors",
                    "2",
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(wait_durations, [3, 7, 3])
        self.assertNotIn("database-url-secret", result.output)
        self.assertNotIn("operation-secret-id", result.output)
        self.assertNotIn("plan-digest", result.output)
        self.assertNotIn("execute-payload", result.output)
        telemetry = [
            json.loads(line) for line in result.output.splitlines() if line.startswith("{")
        ]
        self.assertEqual(
            [entry["event"] for entry in telemetry],
            [
                "privileged_operation_worker_started",
                "privileged_operation_worker_store_build_started",
                "privileged_operation_worker_retry",
                "privileged_operation_worker_schema_probe_succeeded",
                "privileged_operation_worker_store_initialized",
                "privileged_operation_worker_first_poll_attempted",
                "privileged_operation_worker_poll_succeeded",
                "privileged_operation_worker_retry",
                "privileged_operation_worker_threshold_exit",
            ],
        )
        self.assertEqual(telemetry[2]["consecutive_errors"], 1)
        self.assertEqual(telemetry[2]["error_type"], "RuntimeError")
        self.assertEqual(
            telemetry[6],
            {
                "event": "privileged_operation_worker_poll_succeeded",
                "processed": 1,
                "statuses": ["executed"],
            },
        )
        self.assertEqual(telemetry[7]["consecutive_errors"], 1)
        self.assertEqual(telemetry[8]["consecutive_errors"], 2)

    def test_heartbeat_write_failure_counts_as_poll_failure(self) -> None:
        class TestStopEvent:
            def is_set(self) -> bool:
                return False

            def wait(self, _timeout: int) -> bool:
                return False

        runner = CliRunner()
        with (
            patch("control_plane.cli_service.Event", return_value=TestStopEvent()),
            patch(
                "control_plane.cli_service.build_privileged_operation_worker_store",
                side_effect=lambda **kwargs: (
                    kwargs["on_schema_probe_succeeded"](),
                    _WorkerStore(fail_heartbeat=True),
                )[1],
            ),
            patch(
                "control_plane.cli_service.execute_approved_privileged_operations_once",
                return_value=[],
            ),
            patch(
                "control_plane.cli_service._consume_privileged_operation_worker_schema_probe_evidence"
            ),
            patch("control_plane.cli_service.signal.signal", return_value=object()),
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "privileged-operation-workers",
                    "run",
                    "--database-url",
                    "sqlite+pysqlite:///:memory:",
                    "--schema-probe-fd",
                    "3",
                    "--max-consecutive-errors",
                    "1",
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        telemetry = [
            json.loads(line) for line in result.output.splitlines() if line.startswith("{")
        ]
        self.assertIn("privileged_operation_worker_threshold_exit", str(telemetry))
        self.assertNotIn("privileged_operation_worker_poll_succeeded", str(telemetry))

    def test_worker_loop_stops_cleanly_after_sigterm(self) -> None:
        signal_handlers: dict[int, object] = {}
        signal_calls: list[tuple[int, object]] = []
        previous_handlers: dict[int, object] = {
            signal.SIGTERM: object(),
            signal.SIGINT: object(),
        }

        class TestStopEvent:
            stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def set(self) -> None:
                self.stopped = True

            def wait(self, _timeout: int) -> bool:
                handler = signal_handlers[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)
                return True

        def record_signal(signum: int, handler: object) -> object:
            signal_calls.append((signum, handler))
            if callable(handler):
                signal_handlers[signum] = handler
                return previous_handlers[signum]
            return object()

        runner = CliRunner()
        with (
            patch("control_plane.cli_service.Event", return_value=TestStopEvent()),
            patch(
                "control_plane.cli_service.build_privileged_operation_worker_store",
                side_effect=_build_worker_store_with_successful_probe,
            ),
            patch(
                "control_plane.cli_service.execute_approved_privileged_operations_once",
                return_value=[
                    SimpleNamespace(operation_id="operation-secret-id", status="executed")
                ],
            ),
            patch(
                "control_plane.cli_service._consume_privileged_operation_worker_schema_probe_evidence"
            ),
            patch("control_plane.cli_service.signal.signal", side_effect=record_signal),
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "privileged-operation-workers",
                    "run",
                    "--database-url",
                    "sqlite+pysqlite:///:memory:",
                    "--schema-probe-fd",
                    "3",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("operation-secret-id", result.output)
        telemetry = [
            json.loads(line) for line in result.output.splitlines() if line.startswith("{")
        ]
        self.assertEqual(
            [entry["event"] for entry in telemetry],
            [
                "privileged_operation_worker_started",
                "privileged_operation_worker_store_build_started",
                "privileged_operation_worker_schema_probe_succeeded",
                "privileged_operation_worker_store_initialized",
                "privileged_operation_worker_first_poll_attempted",
                "privileged_operation_worker_poll_succeeded",
                "privileged_operation_worker_stopped",
            ],
        )
        self.assertEqual(signal_calls[-2:], list(previous_handlers.items()))

    def test_worker_loop_preserves_base_exception_and_restores_signal_handlers(self) -> None:
        class FatalWorkerError(BaseException):
            pass

        class TestStopEvent:
            def is_set(self) -> bool:
                return False

        signal_calls: list[tuple[int, object]] = []
        previous_handlers: dict[int, object] = {
            signal.SIGTERM: object(),
            signal.SIGINT: object(),
        }

        def record_signal(signum: int, handler: object) -> object:
            signal_calls.append((signum, handler))
            if callable(handler):
                return previous_handlers[signum]
            return object()

        runner = CliRunner()
        with (
            patch("control_plane.cli_service.Event", return_value=TestStopEvent()),
            patch(
                "control_plane.cli_service.build_privileged_operation_worker_store",
                side_effect=_build_worker_store_with_successful_probe,
            ),
            patch(
                "control_plane.cli_service.execute_approved_privileged_operations_once",
                side_effect=FatalWorkerError("operation-secret-id must not be emitted"),
            ),
            patch(
                "control_plane.cli_service._consume_privileged_operation_worker_schema_probe_evidence"
            ),
            patch("control_plane.cli_service.signal.signal", side_effect=record_signal),
            patch("control_plane.cli_service.click.echo") as echo,
        ):
            with self.assertRaises(FatalWorkerError):
                runner.invoke(
                    main,
                    [
                        "service",
                        "privileged-operation-workers",
                        "run",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                        "--schema-probe-fd",
                        "3",
                    ],
                )

        telemetry = [json.loads(call.args[0]) for call in echo.call_args_list]
        self.assertEqual(
            telemetry,
            [
                {
                    "event": "privileged_operation_worker_started",
                    "limit": 20,
                    "poll_seconds": 15,
                },
                {"event": "privileged_operation_worker_store_build_started"},
                {"event": "privileged_operation_worker_schema_probe_succeeded"},
                {"event": "privileged_operation_worker_store_initialized"},
                {"event": "privileged_operation_worker_first_poll_attempted"},
            ],
        )
        self.assertEqual(signal_calls[-2:], list(previous_handlers.items()))

    def test_worker_lifecycle_markers_are_not_repeated_across_successful_polls(self) -> None:
        class TestStopEvent:
            stopped = False
            waits = 0

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, _timeout: int) -> bool:
                self.waits += 1
                if self.waits == 2:
                    self.stopped = True
                return self.stopped

        runner = CliRunner()
        with (
            patch("control_plane.cli_service.Event", return_value=TestStopEvent()),
            patch(
                "control_plane.cli_service.build_privileged_operation_worker_store",
                side_effect=_build_worker_store_with_successful_probe,
            ),
            patch(
                "control_plane.cli_service.execute_approved_privileged_operations_once",
                return_value=[],
            ) as execute_once,
            patch(
                "control_plane.cli_service._consume_privileged_operation_worker_schema_probe_evidence"
            ),
            patch("control_plane.cli_service.signal.signal", return_value=object()),
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "privileged-operation-workers",
                    "run",
                    "--database-url",
                    "sqlite+pysqlite:///:memory:",
                    "--schema-probe-fd",
                    "3",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        telemetry = [
            json.loads(line) for line in result.output.splitlines() if line.startswith("{")
        ]
        self.assertEqual(
            [entry["event"] for entry in telemetry],
            [
                "privileged_operation_worker_started",
                "privileged_operation_worker_store_build_started",
                "privileged_operation_worker_schema_probe_succeeded",
                "privileged_operation_worker_store_initialized",
                "privileged_operation_worker_first_poll_attempted",
                "privileged_operation_worker_poll_succeeded",
                "privileged_operation_worker_poll_succeeded",
                "privileged_operation_worker_stopped",
            ],
        )
        self.assertEqual(execute_once.call_count, 2)

    def test_worker_reauthorizes_by_github_id_and_executes_only_once(self) -> None:
        approval_policy = _policy_record(revision=3)
        active_policy = _policy_record(
            revision=9,
            logins=("renamed-operator",),
            roles=("read_only",),
        )
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                operation_id, secret_id = _prepare_approved_operation(
                    store,
                    approval_policy=approval_policy,
                )
                with (
                    patch.dict(os.environ, self._execution_environment(), clear=True),
                    patch(
                        "control_plane.privileged_operation_worker.read_active_authz_policy_record",
                        return_value=active_policy,
                    ),
                    patch(
                        "control_plane.privileged_operation_worker.control_plane_secrets.reencrypt_secrets",
                        wraps=control_plane_secrets.reencrypt_secrets,
                    ) as executor,
                ):
                    first = execute_approved_privileged_operations_once(
                        record_store=store,
                        lease_owner="worker-1",
                        now=lambda: FIXED_NOW,
                    )
                    second = execute_approved_privileged_operations_once(
                        record_store=store,
                        lease_owner="worker-2",
                        now=lambda: FIXED_NOW,
                    )

                current = store.read_privileged_operation_record(operation_id)
                secret = store.read_secret_record(secret_id)
                version = store.read_secret_version(secret.current_version_id)
                lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=privileged_operation_execution_fingerprint(current),
                )
            finally:
                store.close()

        self.assertEqual(tuple(record.status for record in first), ("executed",))
        self.assertEqual(second, ())
        self.assertEqual(current.status, "executed")
        self.assertEqual(version.key_id, "key-2")
        apply_calls = [call for call in executor.call_args_list if call.kwargs.get("apply")]
        self.assertEqual(len(apply_calls), 1)
        self.assertTrue(executor.call_args.kwargs["apply"])
        self.assertEqual(
            executor.call_args.kwargs["operation_token"],
            privileged_operation_execution_token(operation_id),
        )
        self.assertEqual(lookup.status, "found")
        self.assertIsNotNone(lookup.record)
        reservation = lookup.record
        assert reservation is not None
        self.assertEqual(reservation.state, "completed")
        self.assertEqual(
            reservation.provider_target_key,
            privileged_operation_provider_target_key(current),
        )

    def test_stale_execution_recovers_completed_effect_by_operation_token(self) -> None:
        approval_policy = _policy_record(revision=3)
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                operation_id, secret_id = _prepare_approved_operation(
                    store,
                    approval_policy=approval_policy,
                )
                original_transition = store.transition_privileged_operation

                def crash_after_effect(
                    record: PrivilegedOperationRecord,
                    event: PrivilegedOperationEventRecord,
                ) -> PrivilegedOperationEventWriteStatus:
                    if record.status == "executed":
                        raise KeyboardInterrupt("simulated worker crash")
                    return original_transition(record, event)

                with (
                    patch.dict(os.environ, self._execution_environment(), clear=True),
                    patch(
                        "control_plane.privileged_operation_worker.read_active_authz_policy_record",
                        return_value=approval_policy,
                    ),
                    patch.object(
                        store,
                        "transition_privileged_operation",
                        side_effect=crash_after_effect,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )

                executing = store.read_privileged_operation_record(operation_id)
                fingerprint = privileged_operation_execution_fingerprint(executing)
                lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=fingerprint,
                )
                self.assertIsNotNone(lookup.record)
                reservation = lookup.record
                assert reservation is not None
                store.mark_mutation_reconcile_required(
                    reservation=reservation,
                    reconciliation_key=operation_id,
                )
                contender = store.reserve_mutation(
                    scope="privileged-operation-execution",
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key="competing-operation",
                    request_fingerprint="f" * 64,
                    lease_owner="worker-2",
                    reconciliation_key="competing-operation",
                    provider_target_key=privileged_operation_provider_target_key(executing),
                )

                with (
                    patch.dict(os.environ, self._execution_environment(), clear=True),
                    patch(
                        "control_plane.privileged_operation_worker.read_active_authz_policy_record",
                        return_value=approval_policy,
                    ),
                ):
                    recovered = execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )

                current = store.read_privileged_operation_record(operation_id)
                secret = store.read_secret_record(secret_id)
                version = store.read_secret_version(secret.current_version_id)
                completed_lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=privileged_operation_execution_fingerprint(current),
                )
            finally:
                store.close()

        self.assertEqual(executing.status, "executing")
        self.assertEqual(contender.status, "target_busy")
        self.assertEqual(tuple(record.status for record in recovered), ("executed",))
        self.assertEqual(current.status, "executed")
        self.assertEqual(version.key_id, "key-2")
        self.assertIsNotNone(current.execution)
        execution = current.execution
        assert execution is not None
        self.assertFalse(execution.reconciliation_required)
        self.assertIsNotNone(completed_lookup.record)
        completed_reservation = completed_lookup.record
        assert completed_reservation is not None
        self.assertEqual(completed_reservation.state, "completed")

    def test_stale_policy_execution_recovers_from_inner_cas_readback(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(_policy_admin_record())
                operation_id = _prepare_approved_policy_operation(
                    store,
                    approval_policy=approval_policy,
                )
                original_transition = store.transition_privileged_operation

                def crash_after_policy_effect(
                    record: PrivilegedOperationRecord,
                    event: PrivilegedOperationEventRecord,
                ) -> PrivilegedOperationEventWriteStatus:
                    if record.status == "executed":
                        raise KeyboardInterrupt("simulated policy worker crash")
                    return original_transition(record, event)

                with (
                    patch.object(
                        store,
                        "transition_privileged_operation",
                        side_effect=crash_after_policy_effect,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )

                executing = store.read_privileged_operation_record(operation_id)
                outer_lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=privileged_operation_execution_fingerprint(executing),
                )
                self.assertIsNotNone(outer_lookup.record)
                outer_reservation = outer_lookup.record
                assert outer_reservation is not None
                store.mark_mutation_reconcile_required(
                    reservation=outer_reservation,
                    reconciliation_key=operation_id,
                )

                recovered = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                current = store.read_privileged_operation_record(operation_id)
                active_records = store.list_authz_policy_records(status="active", limit=2)
            finally:
                store.close()

        self.assertEqual(executing.status, "executing")
        self.assertEqual([record.operation_id for record in recovered], [operation_id])
        self.assertEqual(current.status, "executed")
        self.assertIsInstance(current.execution, ManagedAuthzPolicySetExecutionEvidence)
        assert isinstance(current.execution, ManagedAuthzPolicySetExecutionEvidence)
        self.assertFalse(current.execution.reconciliation_required)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 2)

    def test_stale_merge_train_policy_execution_recovers_from_inner_cas_readback(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                approval_policy = store.seed_authz_policy_if_absent(
                    _merge_train_policy_admin_record()
                )
                operation_id = _prepare_approved_merge_train_policy_import(
                    store,
                    approval_policy=approval_policy,
                )
                original_transition = store.transition_privileged_operation

                def crash_after_policy_effect(
                    record: PrivilegedOperationRecord,
                    event: PrivilegedOperationEventRecord,
                ) -> PrivilegedOperationEventWriteStatus:
                    if record.status == "executed":
                        raise KeyboardInterrupt("simulated merge-train policy worker crash")
                    return original_transition(record, event)

                with (
                    patch.object(
                        store,
                        "transition_privileged_operation",
                        side_effect=crash_after_policy_effect,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )

                executing = store.read_privileged_operation_record(operation_id)
                outer_lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=privileged_operation_execution_fingerprint(executing),
                )
                self.assertIsNotNone(outer_lookup.record)
                outer_reservation = outer_lookup.record
                assert outer_reservation is not None
                store.mark_mutation_reconcile_required(
                    reservation=outer_reservation,
                    reconciliation_key=operation_id,
                )

                recovered = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW,
                )

                current = store.read_privileged_operation_record(operation_id)
                active_records = store.list_merge_train_policy_records(status="active", limit=2)
                superseded_records = store.list_merge_train_policy_records(
                    status="superseded",
                    limit=10,
                )
            finally:
                store.close()

        self.assertEqual(executing.status, "executing")
        self.assertEqual([record.operation_id for record in recovered], [operation_id])
        self.assertEqual(current.status, "executed")
        self.assertIsInstance(current.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        assert isinstance(current.execution, ManagedMergeTrainPolicyImportExecutionEvidence)
        self.assertFalse(current.execution.reconciliation_required)
        self.assertEqual(
            [record.record_id for record in active_records],
            ["merge-train-policy-candidate"],
        )
        self.assertEqual(
            [record.record_id for record in superseded_records],
            ["merge-train-policy-active"],
        )

    def test_removed_github_id_rule_fails_before_effect_without_reconciliation(self) -> None:
        approval_policy = _policy_record(revision=3)
        active_policy = _policy_record(revision=4, github_ids=(999,))
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                operation_id, secret_id = _prepare_approved_operation(
                    store,
                    approval_policy=approval_policy,
                )
                with (
                    patch.dict(os.environ, self._execution_environment(), clear=True),
                    patch(
                        "control_plane.privileged_operation_worker.read_active_authz_policy_record",
                        return_value=active_policy,
                    ),
                    patch(
                        "control_plane.privileged_operation_worker.control_plane_secrets.reencrypt_secrets"
                    ) as executor,
                ):
                    results = execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )
                current = store.read_privileged_operation_record(operation_id)
                secret = store.read_secret_record(secret_id)
                version = store.read_secret_version(secret.current_version_id)
            finally:
                store.close()

        executor.assert_not_called()
        self.assertEqual(tuple(record.status for record in results), ("execution_failed",))
        self.assertIsNotNone(current.execution)
        execution = current.execution
        assert execution is not None
        self.assertEqual(execution.failure_code, "approval_managed_rule_drift")
        self.assertFalse(execution.reconciliation_required)
        self.assertEqual(version.key_id, "key-1")

    def test_fresh_plan_drift_fails_before_effect(self) -> None:
        approval_policy = _policy_record(revision=3)
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                operation_id, _secret_id = _prepare_approved_operation(
                    store,
                    approval_policy=approval_policy,
                )
                approved = store.read_privileged_operation_record(operation_id)
                changed_evidence = approved.evidence.model_copy(update={"plan_digest": "f" * 64})
                registration = RegisteredPrivilegedOperationDescriptor(
                    descriptor=MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
                    planner=lambda _store, _request: changed_evidence,
                )
                with (
                    patch.dict(os.environ, self._execution_environment(), clear=True),
                    patch(
                        "control_plane.privileged_operation_worker.read_active_authz_policy_record",
                        return_value=approval_policy,
                    ),
                    patch(
                        "control_plane.privileged_operation_worker.read_privileged_operation_descriptor",
                        return_value=registration,
                    ),
                    patch(
                        "control_plane.privileged_operation_worker.control_plane_secrets.reencrypt_secrets"
                    ) as executor,
                ):
                    execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )
                current = store.read_privileged_operation_record(operation_id)
            finally:
                store.close()

        executor.assert_not_called()
        self.assertEqual(current.status, "execution_failed")
        self.assertIsNotNone(current.execution)
        execution = current.execution
        assert execution is not None
        self.assertEqual(execution.failure_code, "approved_plan_drift")
        self.assertFalse(execution.reconciliation_required)

    def test_failure_after_effect_marks_reconciliation_and_redacts_error(self) -> None:
        approval_policy = _policy_record(revision=3)
        raw_failure = "database failure containing super-secret-value"
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            try:
                operation_id, _secret_id = _prepare_approved_operation(
                    store,
                    approval_policy=approval_policy,
                )
                original_transition = store.transition_privileged_operation

                def fail_terminal_success(
                    record: PrivilegedOperationRecord,
                    event: PrivilegedOperationEventRecord,
                ) -> PrivilegedOperationEventWriteStatus:
                    if record.status == "executed":
                        raise RuntimeError(raw_failure)
                    return original_transition(record, event)

                with (
                    patch.dict(os.environ, self._execution_environment(), clear=True),
                    patch(
                        "control_plane.privileged_operation_worker.read_active_authz_policy_record",
                        return_value=approval_policy,
                    ),
                    patch.object(
                        store,
                        "transition_privileged_operation",
                        side_effect=fail_terminal_success,
                    ),
                ):
                    execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW,
                    )
                current = store.read_privileged_operation_record(operation_id)
                lookup = store.lookup_existing_mutation_reservation(
                    route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
                    idempotency_key=operation_id,
                    request_fingerprint=privileged_operation_execution_fingerprint(current),
                )
            finally:
                store.close()

        rendered = current.model_dump_json()
        self.assertEqual(current.status, "execution_failed")
        self.assertIsNotNone(current.execution)
        execution = current.execution
        assert execution is not None
        self.assertTrue(execution.reconciliation_required)
        self.assertEqual(
            execution.failure_code,
            "privileged_operation_execution_error",
        )
        self.assertNotIn("super-secret-value", rendered)
        self.assertIsNotNone(lookup.record)
        reservation = lookup.record
        assert reservation is not None
        self.assertEqual(reservation.state, "reconcile_required")


if __name__ == "__main__":
    unittest.main()
