from collections.abc import Mapping

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy


def durable_operation_authorization_payload(
    *,
    action: str,
    managed_rule_id: str,
    product: str = "odoo-tenant-cm",
    context: str = "cm",
    instances: tuple[str, ...] = ("testing",),
    managed_set_id: str = "operator.odoo-stable-operations",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": action,
        "product": product,
        "context": context,
        "instances": list(instances),
        "managed_set_id": managed_set_id,
        "managed_rule_id": managed_rule_id,
        "policy_record_id": "launchplane-authz-policy-r00000000000000000041-example",
        "policy_revision": 41,
        "policy_schema_version": 2,
        "policy_sha256": "a" * 64,
        "policy_source": "service:test",
        "authorized_at": "2026-07-23T03:31:00Z",
        "caller": {
            "schema_version": 1,
            "identity_type": "github_actions",
            "subject": "repo:cbusillo/launchplane:ref:refs/heads/main",
            "repository": "cbusillo/launchplane",
            "repository_owner": "cbusillo",
            "repository_id": "1001",
            "repository_owner_id": "2001",
            "workflow_ref": (
                "cbusillo/launchplane/.github/workflows/odoo-stable-operation.yml@refs/heads/main"
            ),
            "job_workflow_ref": (
                "cbusillo/launchplane/.github/workflows/reusable-odoo-stable-operation.yml@"
                "0123456789abcdef0123456789abcdef01234567"
            ),
            "ref": "refs/heads/main",
            "ref_type": "branch",
            "event_name": "workflow_dispatch",
            "environment": "",
            "sha": "0123456789abcdef0123456789abcdef01234567",
        },
    }


def durable_operation_policy_record(
    *authorizations: Mapping[str, object],
    revision: int = 42,
) -> LaunchplaneAuthzPolicyRecord:
    rules: list[dict[str, object]] = []
    for authorization in authorizations:
        caller = authorization["caller"]
        if not isinstance(caller, Mapping) or caller.get("identity_type") != "github_actions":
            raise ValueError("Test durable operation policy requires GitHub Actions callers.")
        rules.append(
            {
                "managed_set_id": authorization["managed_set_id"],
                "managed_rule_id": authorization["managed_rule_id"],
                "repository": caller["repository"],
                "repository_id": caller["repository_id"],
                "repository_owner_id": caller["repository_owner_id"],
                "workflow_refs": [caller["workflow_ref"]],
                "job_workflow_refs": [caller["job_workflow_ref"]],
                "event_names": [caller["event_name"]],
                "refs": [caller["ref"]],
                "products": [authorization["product"]],
                "contexts": [authorization["context"]],
                "instances": authorization["instances"],
                "actions": [authorization["action"]],
            }
        )
    policy = LaunchplaneAuthzPolicy.model_validate({"schema_version": 2, "github_actions": rules})
    return LaunchplaneAuthzPolicyRecord(
        record_id=f"launchplane-authz-policy-r{revision}",
        revision=revision,
        status="active",
        source="service:test",
        updated_at="2026-07-23T03:33:00Z",
        policy=policy,
    )


def durable_operation_cancellation_payload() -> dict[str, object]:
    authorization = durable_operation_authorization_payload(
        action="odoo_stable_bootstrap.execute",
        managed_rule_id="cm-testing-bootstrap",
    )
    return {
        "schema_version": 1,
        "reason": "Operator cancelled pending destructive work.",
        "cancelled_at": "2026-07-23T03:32:00Z",
        "caller": authorization["caller"],
    }
