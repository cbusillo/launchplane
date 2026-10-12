from __future__ import annotations

from datetime import datetime, timezone
import unittest

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
)
from control_plane.contracts.privileged_operation import (
    PrivilegedOperationActor,
    PrivilegedOperationApproval,
    PrivilegedOperationRecord,
    privileged_operation_pre_state_digest,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


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


if __name__ == "__main__":
    unittest.main()
