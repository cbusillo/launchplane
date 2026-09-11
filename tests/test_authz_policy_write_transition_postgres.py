from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import threading
import time
import unittest
from unittest.mock import patch

from control_plane.authz_grant_service import (
    AuthzManagedPolicyRouteResult,
    execute_managed_authz_policy_reconcile,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicyImmutableHumanCallerBinding,
    AuthzPolicySchemaV3MaintenanceEvidence,
    AuthzPolicySchemaV3TransitionDeniedError,
    AuthzPolicySchemaV3WriteEvidence,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationEvent,
    OrdinaryAgentDeliveryActivationExecutionEvidence,
    OrdinaryAgentDeliveryActivationRecord,
    OrdinaryAgentDeliveryActivationSetupRequest,
    build_ordinary_agent_delivery_activation_event_id,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetProposalInput,
    PrivilegedOperationActor,
    PrivilegedOperationRecord,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.solo_administration_confirmation import (
    SoloAdministrationConfirmationConsumptionBinding,
    issue_solo_administration_confirmation,
)
from control_plane.privileged_operation_service import (
    approve_privileged_operation,
    cancel_privileged_operation,
    create_typed_privileged_operation_plan,
)
from control_plane.privileged_operation_worker import (
    execute_approved_privileged_operations_once,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from tests.test_ordinary_agent_activation_worker import _active_policy, _approval
from tests.test_postgres_integration import _store_for_fresh_head_database


TEST_NOW = datetime.now(UTC).replace(microsecond=0)


@dataclass(frozen=True)
class _PreparedActivation:
    initial_policy: LaunchplaneAuthzPolicyRecord
    policy_operation: PrivilegedOperationRecord
    activation: OrdinaryAgentDeliveryActivationRecord
    route: AuthzManagedPolicyRouteResult


class _PausedEvidenceStore(PostgresRecordStore):
    def __init__(
        self,
        *,
        database_url: str,
        evidence_boundary_entered: threading.Event,
        resume_evidence_check: threading.Event,
    ) -> None:
        super().__init__(database_url=database_url)
        self._evidence_boundary_entered = evidence_boundary_entered
        self._resume_evidence_check = resume_evidence_check

    def _require_authz_policy_schema_v3_write_evidence_locked(
        self,
        *,
        session: object,
        current_record: LaunchplaneAuthzPolicyRecord,
        replacement_record: LaunchplaneAuthzPolicyRecord | None,
        evidence: AuthzPolicySchemaV3WriteEvidence | None,
    ) -> None:
        self._evidence_boundary_entered.set()
        if not self._resume_evidence_check.wait(timeout=10):
            raise TimeoutError("test did not release the schema-v3 evidence boundary")
        super()._require_authz_policy_schema_v3_write_evidence_locked(
            session=session,
            current_record=current_record,
            replacement_record=replacement_record,
            evidence=evidence,
        )


def _prepare_executed_activation(store: PostgresRecordStore) -> _PreparedActivation:
    initial_policy = LaunchplaneAuthzPolicyRecord(
        record_id="launchplane-authz-policy-r1",
        revision=1,
        source="test:a5-postgres",
        updated_at=TEST_NOW.isoformat(),
        policy=_active_policy(),
    )
    initial_policy = store.seed_authz_policy_if_absent(initial_policy)
    inventory = RepositoryInventoryRecord(
        repository_id="1001",
        repository_owner_id="9001",
        repository="example/launchplane",
        inventory_state="tracked",
        inventory_revision=1,
        recorded_at=TEST_NOW.isoformat(),
        source="test:a5-postgres",
        reason="Bind the exact PostgreSQL activation target.",
    )
    store.write_repository_inventory_record(inventory)
    target = OrdinaryAgentTarget(
        repository_id=1001,
        repository="example/launchplane",
        base_branch="main",
    )
    policy_operation = create_typed_privileged_operation_plan(
        record_store=store,
        descriptor_id="managed-authz-policy-set",
        actor=PrivilegedOperationActor(
            identity_type="github_human",
            github_id=101,
            login="activation-reviewer",
        ),
        source_kind="browser_api",
        source_event_id="a5-postgres-policy-package",
        request=ManagedAuthzPolicySetProposalInput(
            managed_set_id="ordinary-agent.pilot",
            schema_migration="migrate_v2_to_v3",
            reason="Propose one future ordinary-agent rule.",
            desired_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 3,
                    "ordinary_agents": [
                        {
                            "managed_set_id": "ordinary-agent.pilot",
                            "managed_rule_id": "agent-one",
                            "principal_id": "agent_one",
                            "target": target.model_dump(mode="json"),
                            "actions": ["self_read", "preflight"],
                        }
                    ],
                }
            ),
        ),
        now=lambda: TEST_NOW,
    ).record
    setup_operation = create_typed_privileged_operation_plan(
        record_store=store,
        descriptor_id="ordinary-agent-delivery-activation",
        actor=PrivilegedOperationActor(
            identity_type="github_human",
            github_id=101,
            login="activation-reviewer",
        ),
        source_kind="browser_api",
        source_event_id="a5-postgres-activation-setup",
        request=OrdinaryAgentDeliveryActivationSetupRequest(
            policy_operation_id=policy_operation.operation_id,
            repository_inventory_record_id=inventory.record_id,
            activation_expires_at=(TEST_NOW + timedelta(days=1)).isoformat(),
            reason="Prepare qualification-only delivery.",
        ),
        now=lambda: TEST_NOW + timedelta(minutes=1),
    ).record
    approve_privileged_operation(
        record_store=store,
        operation_id=setup_operation.operation_id,
        approval=_approval(setup_operation, policy_record=initial_policy),
        source_event_id="a5-postgres-approve-setup",
        now=lambda: TEST_NOW + timedelta(minutes=2),
    )
    with (
        patch("control_plane.privileged_operation_worker.control_plane_secrets.reencrypt_secrets"),
        patch(
            "control_plane.privileged_operation_worker.authz_grant_service.execute_managed_authz_policy_reconcile"
        ),
        patch("control_plane.ordinary_agent_activation.datetime", wraps=datetime) as clock,
    ):
        clock.now.return_value = TEST_NOW + timedelta(minutes=3)
        setup_results = execute_approved_privileged_operations_once(
            record_store=store,
            now=lambda: TEST_NOW + timedelta(minutes=3),
        )
    if len(setup_results) != 1 or setup_results[0].status != "executed":
        raise AssertionError(f"activation setup did not execute: {setup_results!r}")
    execution = setup_results[0].execution
    if not isinstance(execution, OrdinaryAgentDeliveryActivationExecutionEvidence):
        raise AssertionError(f"activation setup returned unexpected evidence: {execution!r}")
    activation = store.read_ordinary_agent_delivery_activation_record(execution.activation_id)
    if not isinstance(policy_operation.request, ManagedAuthzPolicySetProposalInput):
        raise AssertionError("managed policy package request changed type")
    route = execute_managed_authz_policy_reconcile(
        record_store=store,
        request=policy_operation.request.reconcile_request(
            mode="apply",
            reviewed_plan_sha256=policy_operation.evidence.plan_digest,
        ),
        identity=GitHubHumanIdentity(
            login="activation-reviewer",
            github_id=101,
            name="Activation Reviewer",
            email="activation-reviewer@example.test",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        ),
        immutable_applying_github_id=101,
        trace_id="launchplane_req_a5_postgres_apply",
        now_timestamp=lambda: (TEST_NOW + timedelta(minutes=4)).isoformat(),
    )
    if not route.changed or route.schema_v3_write_evidence is None:
        raise AssertionError("executed setup did not produce schema-v3 write evidence")
    return _PreparedActivation(initial_policy, policy_operation, activation, route)


def _revocation(
    activation: OrdinaryAgentDeliveryActivationRecord,
    *,
    source_operation_id: str,
) -> tuple[OrdinaryAgentDeliveryActivationRecord, OrdinaryAgentDeliveryActivationEvent]:
    occurred_at = (TEST_NOW + timedelta(minutes=5)).isoformat()
    revoked = OrdinaryAgentDeliveryActivationRecord.model_validate(
        {
            **activation.model_dump(mode="json"),
            "desired_state": "revoked",
            "effective_state": "revoked",
            "revision": activation.revision + 1,
            "updated_at": occurred_at,
            "revoked_at": occurred_at,
            "activation_sha256": "",
        }
    )
    event = OrdinaryAgentDeliveryActivationEvent(
        event_id=build_ordinary_agent_delivery_activation_event_id(
            activation_id=revoked.activation_id,
            sequence=revoked.revision,
            action="revoked",
            source_operation_id=source_operation_id,
        ),
        activation_id=revoked.activation_id,
        sequence=revoked.revision,
        action="revoked",
        previous_revision=activation.revision,
        previous_activation_sha256=activation.activation_sha256,
        resulting_revision=revoked.revision,
        resulting_activation_sha256=revoked.activation_sha256,
        resulting_desired_state=revoked.desired_state,
        resulting_effective_state=revoked.effective_state,
        occurred_at=occurred_at,
        source_operation_id=source_operation_id,
    )
    return revoked, event


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _mutation_with_confirmation(
    store: PostgresRecordStore,
    prepared: _PreparedActivation,
    *,
    suffix: str,
) -> tuple[DbOnlyMutationRequest, str]:
    scope = "github-human|101"
    route_path = "/v1/authz-policies/reconcile"
    idempotency_key = f"a5-postgres-{suffix}"
    session_digest = _sha256(f"session-{suffix}")
    acknowledgement_digest = _sha256(f"acknowledgement-{suffix}")
    secret_digest = _sha256(f"secret-{suffix}")
    confirmation = issue_solo_administration_confirmation(
        active_policy_record_id=prepared.initial_policy.record_id,
        active_policy_revision=prepared.initial_policy.revision,
        active_policy_sha256=prepared.initial_policy.policy_sha256,
        candidate_policy_sha256=prepared.route.authz_policy_record.policy_sha256,
        reviewed_plan_sha256=prepared.policy_operation.evidence.plan_digest,
        human_session_id_sha256=session_digest,
        github_id=101,
        idempotency_scope_sha256=_sha256(scope),
        idempotency_key_sha256=_sha256(idempotency_key),
        acknowledgement_sha256=acknowledgement_digest,
        secret_sha256=secret_digest,
        created_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )
    store.issue_solo_administration_confirmation(confirmation)
    binding = SoloAdministrationConfirmationConsumptionBinding(
        confirmation_id=confirmation.confirmation_id,
        active_policy_record_id=confirmation.active_policy_record_id,
        active_policy_revision=confirmation.active_policy_revision,
        active_policy_sha256=confirmation.active_policy_sha256,
        candidate_policy_sha256=confirmation.candidate_policy_sha256,
        reviewed_plan_sha256=confirmation.reviewed_plan_sha256,
        human_session_id_sha256=confirmation.human_session_id_sha256,
        github_id=confirmation.github_id,
        idempotency_scope_sha256=confirmation.idempotency_scope_sha256,
        idempotency_key_sha256=confirmation.idempotency_key_sha256,
        acknowledgement_sha256=confirmation.acknowledgement_sha256,
        secret_sha256=confirmation.secret_sha256,
    )
    return (
        DbOnlyMutationRequest(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=f"fingerprint-{suffix}",
            lease_owner=f"writer-{suffix}",
            response_status_code=202,
            response_trace_id=f"trace-{suffix}",
            response_payload={"status": "accepted", "case": suffix},
            confirmation_consumption=binding,
            lease_seconds=30,
        ),
        confirmation.confirmation_id,
    )


def _write_candidate(
    store: PostgresRecordStore,
    prepared: _PreparedActivation,
    mutation: DbOnlyMutationRequest,
) -> AuthzPolicyCompareWriteResult:
    return store.compare_and_write_authz_policy_record(
        expected_record=prepared.route.previous_authz_policy_record,
        replacement_record=prepared.route.authz_policy_record,
        schema_v3_write_evidence=prepared.route.schema_v3_write_evidence,
        mutation=mutation,
    )


class AuthzPolicyWriteTransitionPostgresTests(unittest.TestCase):
    def test_executed_setup_enables_v3_replay_survives_revoke_and_removal_is_allowed(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            prepared = _prepare_executed_activation(store)
            mutation, confirmation_id = _mutation_with_confirmation(
                store, prepared, suffix="success"
            )

            written = _write_candidate(store, prepared, mutation)
            self.assertEqual(written.status, "written")
            self.assertEqual(written.current_record, prepared.route.authz_policy_record)
            self.assertEqual(
                store.read_solo_administration_confirmation(confirmation_id).state,
                "consumed",
            )
            revoked, revoked_event = _revocation(
                prepared.activation,
                source_operation_id="a5-postgres-revoke-after-write",
            )
            store.revoke_ordinary_agent_delivery_activation(revoked, revoked_event)

            replayed = _write_candidate(store, prepared, mutation)
            self.assertEqual(replayed.status, "replayed")
            self.assertIsNotNone(replayed.idempotency_record)
            assert replayed.idempotency_record is not None
            self.assertEqual(replayed.idempotency_record.state, "completed")

            current = prepared.route.authz_policy_record
            maintenance_policy = current.policy.model_copy(update={"ordinary_agents": ()})
            maintenance_digest = authz_policy_sha256(maintenance_policy)
            maintenance = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=current.revision + 1,
                    policy_sha256=maintenance_digest,
                ),
                revision=current.revision + 1,
                status="active",
                source="test:a5-postgres-maintenance",
                updated_at=(TEST_NOW + timedelta(minutes=6)).isoformat(),
                policy=maintenance_policy,
            )
            removal = store.compare_and_write_authz_policy_record(
                expected_record=current,
                replacement_record=maintenance,
                schema_v3_write_evidence=AuthzPolicySchemaV3MaintenanceEvidence(
                    caller=AuthzPolicyImmutableHumanCallerBinding(github_id=101),
                    expected_record_id=current.record_id,
                    expected_revision=current.revision,
                    expected_policy_sha256=current.policy_sha256,
                    candidate_policy_sha256=maintenance.policy_sha256,
                ),
            )

            self.assertEqual(removal.status, "written")
            self.assertEqual(removal.current_record, maintenance)
            self.assertEqual(maintenance.policy.schema_version, 3)
            self.assertEqual(maintenance.policy.ordinary_agents, ())

    def test_revoke_wins_at_locked_boundary_without_partial_mutation_commit(self) -> None:
        with _store_for_fresh_head_database() as store:
            prepared = _prepare_executed_activation(store)
            mutation, confirmation_id = _mutation_with_confirmation(
                store, prepared, suffix="revoke-race"
            )
            entered = threading.Event()
            resume = threading.Event()
            writer = _PausedEvidenceStore(
                database_url=store.database_url,
                evidence_boundary_entered=entered,
                resume_evidence_check=resume,
            )
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_write_candidate, writer, prepared, mutation)
                    self.assertTrue(
                        entered.wait(timeout=10), "writer did not reach locked boundary"
                    )
                    revoked, revoked_event = _revocation(
                        prepared.activation,
                        source_operation_id="a5-postgres-racing-revoke",
                    )
                    store.revoke_ordinary_agent_delivery_activation(revoked, revoked_event)
                    resume.set()
                    with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError) as raised:
                        future.result(timeout=10)
            finally:
                resume.set()
                writer.close()

            self.assertEqual(raised.exception.reason_code, "activation_binding_mismatch")
            self.assertEqual(
                store.list_authz_policy_records(status="active"),
                (prepared.initial_policy,),
            )
            self.assertEqual(
                store.read_solo_administration_confirmation(confirmation_id).state,
                "issued",
            )
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=mutation.scope,
                    route_path=mutation.route_path,
                    idempotency_key=mutation.idempotency_key,
                )
            )

    def test_source_cancellation_rolls_back_reclaimed_reservation_and_confirmation(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            prepared = _prepare_executed_activation(store)
            mutation, confirmation_id = _mutation_with_confirmation(
                store, prepared, suffix="source-race"
            )
            first_reservation = store.reserve_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner="original-owner",
                lease_seconds=1,
            )
            self.assertEqual(first_reservation.status, "acquired")
            time.sleep(1.1)
            entered = threading.Event()
            resume = threading.Event()
            writer = _PausedEvidenceStore(
                database_url=store.database_url,
                evidence_boundary_entered=entered,
                resume_evidence_check=resume,
            )
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_write_candidate, writer, prepared, mutation)
                    self.assertTrue(
                        entered.wait(timeout=10), "writer did not reach locked boundary"
                    )
                    cancelled = cancel_privileged_operation(
                        record_store=store,
                        operation_id=prepared.policy_operation.operation_id,
                        actor_github_id=101,
                        actor_login="activation-reviewer",
                        source_event_id="a5-postgres-racing-source-cancel",
                        reason="Cancel the source proposal before its policy write.",
                        now=lambda: TEST_NOW + timedelta(minutes=5),
                    )
                    self.assertEqual(cancelled.record.status, "cancelled")
                    resume.set()
                    with self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError) as raised:
                        future.result(timeout=10)
            finally:
                resume.set()
                writer.close()

            self.assertEqual(raised.exception.reason_code, "policy_source_status_inadmissible")
            self.assertEqual(
                store.list_authz_policy_records(status="active"),
                (prepared.initial_policy,),
            )
            self.assertEqual(
                store.read_solo_administration_confirmation(confirmation_id).state,
                "issued",
            )
            persisted_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
            self.assertIsNotNone(persisted_reservation)
            assert persisted_reservation is not None
            self.assertEqual(persisted_reservation.state, "running")
            self.assertEqual(persisted_reservation.attempt, 1)
            self.assertEqual(persisted_reservation.lease_owner, "original-owner")


if __name__ == "__main__":
    unittest.main()
