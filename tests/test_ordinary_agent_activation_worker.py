from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.authz_grant_service import execute_managed_authz_policy_reconcile
from control_plane.contracts.authz_policy_record import (
    AuthzPolicySchemaWriteNotActivatedError,
    LaunchplaneAuthzPolicyRecord,
    require_authz_policy_schema_write_activated,
)
from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicySchemaV3TransitionDeniedError,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationExecutionEvidence,
    OrdinaryAgentDeliveryActivationRevokeRequest,
    OrdinaryAgentDeliveryActivationSetupRequest,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetProposalInput,
    PrivilegedOperationActor,
    PrivilegedOperationApproval,
    PrivilegedOperationRecord,
    privileged_operation_pre_state_digest,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.privileged_operation_service import (
    approve_privileged_operation,
    create_typed_privileged_operation_plan,
)
from control_plane.privileged_operation_worker import (
    execute_approved_privileged_operations_once,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url


FIXED_NOW = datetime(2099, 9, 10, 21, tzinfo=timezone.utc)


def _active_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "administrator_quorum": 1,
            "github_humans": [
                {
                    "managed_set_id": "ordinary-agent.activation-administrators",
                    "managed_rule_id": "activation-reviewer",
                    "github_ids": [101],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        "authz_policy_grant.write",
                        "authz_policy_operation.approve",
                        "ordinary_agent_delivery_activation.approve",
                    ],
                }
            ],
        }
    )


def _approval(
    record: PrivilegedOperationRecord,
    *,
    policy_record: LaunchplaneAuthzPolicyRecord,
) -> PrivilegedOperationApproval:
    return PrivilegedOperationApproval(
        approver=PrivilegedOperationActor(
            identity_type="github_human",
            github_id=101,
            login="activation-reviewer",
        ),
        descriptor_id=record.descriptor_id,
        descriptor_version=record.descriptor_version,
        request_digest=record.request_digest,
        evidence_digest=record.evidence_digest,
        plan_digest=record.evidence.plan_digest,
        pre_state_digest=privileged_operation_pre_state_digest(record.evidence),
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.revision,
        policy_sha256=policy_record.policy_sha256,
        policy_source=policy_record.source,
        managed_set_id="ordinary-agent.activation-administrators",
        managed_rule_id="activation-reviewer",
        expires_at=record.expires_at,
        reason="Reviewed the qualification-only activation transition.",
        rollback_class=(
            "policy_cas"
            if record.descriptor_id == "managed-authz-policy-set"
            else "activation_revoke"
        ),
    )


class OrdinaryAgentDeliveryActivationWorkerTests(unittest.TestCase):
    def test_worker_installs_qualification_only_then_executes_reviewed_terminal_revoke(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.ordinary_agent_activation.datetime",
                wraps=datetime,
            ) as activation_datetime,
        ):
            activation_datetime.now.return_value = FIXED_NOW
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            try:
                active_policy_record = LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-r1",
                    revision=1,
                    source="test",
                    updated_at=FIXED_NOW.isoformat(),
                    policy=_active_policy(),
                )
                store.seed_authz_policy_if_absent(active_policy_record)
                inventory = RepositoryInventoryRecord(
                    repository_id="1001",
                    repository_owner_id="9001",
                    repository="example/launchplane",
                    inventory_state="tracked",
                    inventory_revision=1,
                    recorded_at=FIXED_NOW.isoformat(),
                    source="test",
                    reason="Bind the exact test repository.",
                )
                store.write_repository_inventory_record(inventory)
                target = OrdinaryAgentTarget(
                    repository_id=1001,
                    repository="example/launchplane",
                    base_branch="main",
                )
                policy_package = create_typed_privileged_operation_plan(
                    record_store=store,
                    descriptor_id="managed-authz-policy-set",
                    actor=PrivilegedOperationActor(
                        identity_type="github_human",
                        github_id=101,
                        login="activation-reviewer",
                    ),
                    source_kind="browser_api",
                    source_event_id="activation-policy-package",
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
                    now=lambda: FIXED_NOW,
                ).record
                setup_plan = create_typed_privileged_operation_plan(
                    record_store=store,
                    descriptor_id="ordinary-agent-delivery-activation",
                    actor=PrivilegedOperationActor(
                        identity_type="github_human",
                        github_id=101,
                        login="activation-reviewer",
                    ),
                    source_kind="browser_api",
                    source_event_id="activation-setup",
                    request=OrdinaryAgentDeliveryActivationSetupRequest(
                        policy_operation_id=policy_package.operation_id,
                        repository_inventory_record_id=inventory.record_id,
                        activation_expires_at=(FIXED_NOW + timedelta(days=1)).isoformat(),
                        reason="Prepare qualification-only delivery.",
                    ),
                    now=lambda: FIXED_NOW + timedelta(minutes=1),
                ).record
                self.assertEqual(setup_plan.evidence.result_status, "ok")
                approve_privileged_operation(
                    record_store=store,
                    operation_id=setup_plan.operation_id,
                    approval=_approval(setup_plan, policy_record=active_policy_record),
                    source_event_id="approve-activation-setup",
                    now=lambda: FIXED_NOW + timedelta(minutes=2),
                )

                with (
                    patch(
                        "control_plane.privileged_operation_worker.control_plane_secrets.reencrypt_secrets"
                    ) as secret_executor,
                    patch(
                        "control_plane.privileged_operation_worker.authz_grant_service.execute_managed_authz_policy_reconcile"
                    ) as policy_executor,
                ):
                    setup_results = execute_approved_privileged_operations_once(
                        record_store=store,
                        now=lambda: FIXED_NOW + timedelta(minutes=3),
                    )
                setup_execution = setup_results[0].execution
                self.assertEqual(setup_results[0].status, "executed", setup_results)
                self.assertIsInstance(
                    setup_execution,
                    OrdinaryAgentDeliveryActivationExecutionEvidence,
                )
                assert isinstance(
                    setup_execution,
                    OrdinaryAgentDeliveryActivationExecutionEvidence,
                )
                activation = store.read_ordinary_agent_delivery_activation_record(
                    setup_execution.activation_id
                )
                self.assertEqual(activation.desired_state, "guarded")
                self.assertEqual(activation.effective_state, "qualification_only")
                self.assertFalse(activation.authorizes_execution)
                secret_executor.assert_not_called()
                policy_executor.assert_not_called()
                assert isinstance(policy_package.request, ManagedAuthzPolicySetProposalInput)

                with (
                    patch.object(
                        store,
                        "ordinary_agent_delivery_activation_schema_capability",
                        return_value=("incompatible", "0" * 64, False),
                    ),
                    self.assertRaises(AuthzPolicySchemaV3TransitionDeniedError),
                ):
                    execute_managed_authz_policy_reconcile(
                        record_store=store,
                        request=policy_package.request.reconcile_request(
                            mode="apply",
                            reviewed_plan_sha256=policy_package.evidence.plan_digest,
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
                        trace_id="launchplane_req_incompatible_runtime",
                        now_timestamp=lambda: (FIXED_NOW + timedelta(minutes=4)).isoformat(),
                    )

                approve_privileged_operation(
                    record_store=store,
                    operation_id=policy_package.operation_id,
                    approval=_approval(policy_package, policy_record=active_policy_record),
                    source_event_id="approve-activation-policy-package",
                    now=lambda: FIXED_NOW + timedelta(minutes=4),
                )
                policy_results = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW + timedelta(minutes=5),
                )
                self.assertEqual(policy_results[0].operation_id, policy_package.operation_id)
                self.assertEqual(policy_results[0].status, "executed", policy_results)
                active_policy_record = store.list_authz_policy_records(status="active", limit=1)[0]
                self.assertEqual(active_policy_record.policy.schema_version, 3)
                self.assertEqual(len(active_policy_record.policy.ordinary_agents), 1)

                revoke_plan = create_typed_privileged_operation_plan(
                    record_store=store,
                    descriptor_id="ordinary-agent-delivery-activation",
                    actor=PrivilegedOperationActor(
                        identity_type="github_human",
                        github_id=101,
                        login="activation-reviewer",
                    ),
                    source_kind="browser_api",
                    source_event_id="activation-stop",
                    request=OrdinaryAgentDeliveryActivationRevokeRequest(
                        activation_id=activation.activation_id,
                        expected_revision=activation.revision,
                        expected_activation_sha256=activation.activation_sha256,
                        reason="Stop this activation intent.",
                    ),
                    now=lambda: FIXED_NOW + timedelta(minutes=6),
                ).record
                approve_privileged_operation(
                    record_store=store,
                    operation_id=revoke_plan.operation_id,
                    approval=_approval(revoke_plan, policy_record=active_policy_record),
                    source_event_id="approve-activation-stop",
                    now=lambda: FIXED_NOW + timedelta(minutes=7),
                )
                revoke_results = execute_approved_privileged_operations_once(
                    record_store=store,
                    now=lambda: FIXED_NOW + timedelta(minutes=8),
                )
                revoked = store.read_ordinary_agent_delivery_activation_record(
                    activation.activation_id
                )
            finally:
                store.close()

        self.assertEqual(setup_results[0].status, "executed")
        self.assertEqual(revoke_results[0].status, "executed", revoke_results)
        self.assertEqual(revoked.desired_state, "revoked")
        self.assertEqual(revoked.effective_state, "revoked")
        self.assertFalse(revoked.authorizes_execution)
        with self.assertRaises(AuthzPolicySchemaWriteNotActivatedError):
            require_authz_policy_schema_write_activated(LaunchplaneAuthzPolicy(schema_version=3))


if __name__ == "__main__":
    unittest.main()
