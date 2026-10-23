import argparse
from pathlib import Path
import runpy

from control_plane.contracts.privileged_operation import ManagedAuthzPolicySetProposalInput
from control_plane.contracts.workflow_rule_retirement import WorkflowRuleRetirementRequest
from control_plane.service_auth import LaunchplaneAuthzPolicy


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare an inert selected-workflow access-removal proposal for Director review."
    )
    parser.add_argument("--operator-helper", type=Path, required=True)
    parser.add_argument("--selection-file", required=True)
    parser.add_argument("--source-event-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--related-issue", default="")
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    helper = args.operator_helper.expanduser().resolve(strict=True)
    if helper.name != "launchplane-write-action.py":
        parser.error("Select the installed Launchplane operator helper.")
    transport = runpy.run_path(str(helper), run_name="workflow_retirement_proposal_transport")
    from control_plane.http_routes.privileged_operations import (
        PrivilegedPolicyOperationAgentProposalEnvelope,
    )

    try:
        selection = WorkflowRuleRetirementRequest.model_validate(
            {**transport["read_payload_file"](args.selection_file), "mode": "plan"}
        )
        proposal = ManagedAuthzPolicySetProposalInput(
            managed_set_id="",
            desired_policy=LaunchplaneAuthzPolicy(schema_version=2),
            workflow_rule_retirement=selection,
            reason=args.reason,
            related_issue=args.related_issue,
        )
        envelope = PrivilegedPolicyOperationAgentProposalEnvelope(
            descriptor_id="managed-authz-policy-set",
            source_event_id=args.source_event_id,
            request=proposal,
        )
    except (KeyError, TypeError, ValueError, OSError):
        transport["emit"](
            transport["unavailable_payload"](
                operation="privileged-policy-propose",
                request={"descriptor_id": "managed-authz-policy-set"},
                status="invalid",
                code="invalid_retirement_proposal",
                message="The private selection and proposal input failed validation.",
            )
        )
        return 2
    args.idempotency_key = ""
    return int(
        transport["execute_post"](
            args=args,
            operation="privileged-policy-propose",
            path=transport["helper_command_path"]("privileged-policy-propose"),
            request={
                "descriptor_id": envelope.descriptor_id,
                "source_event_id": envelope.source_event_id,
                "selected_rule_count": len(selection.selection),
            },
            body=envelope.model_dump(mode="json"),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
