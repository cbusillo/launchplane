from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane import secrets as control_plane_secrets
from control_plane.cli import main
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.privileged_operation import (
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
from control_plane.privileged_operation_registry import (
    MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
    RegisteredPrivilegedOperationDescriptor,
)
from control_plane.privileged_operation_service import (
    approve_privileged_operation,
    create_privileged_operation_plan,
)
from control_plane.privileged_operation_worker import (
    PRIVILEGED_OPERATION_EXECUTION_ROUTE,
    execute_approved_privileged_operations_once,
    privileged_operation_execution_fingerprint,
    privileged_operation_execution_token,
    privileged_operation_provider_target_key,
    require_privileged_operation_execution_store,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url


FIXED_NOW = datetime(2026, 8, 22, 20, 10, tzinfo=timezone.utc)


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

    def test_worker_commands_are_service_internal(self) -> None:
        runner = CliRunner()

        service_help = runner.invoke(main, ["service", "privileged-operation-workers", "--help"])
        top_level = runner.invoke(main, ["privileged-operations", "--help"])

        self.assertEqual(service_help.exit_code, 0)
        self.assertIn("run-once", service_help.output)
        self.assertIn("run", service_help.output)
        self.assertNotEqual(top_level.exit_code, 0)

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
