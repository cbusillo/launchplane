from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_policy import parse_merge_train_policy_toml


def build_test_merge_train_policy(
    *, repository: str = "cbusillo/sellyouroutboard"
) -> MergeTrainPolicy:
    return parse_merge_train_policy_toml(_policy_toml(repository))


def build_test_merge_train_policy_record(
    *,
    repository: str = "cbusillo/sellyouroutboard",
    record_id: str = "merge-train-policy-20260513T210000Z-test",
    updated_at: str = "2026-05-13T21:00:00Z",
) -> MergeTrainPolicyRecord:
    return MergeTrainPolicyRecord(
        record_id=record_id,
        status="active",
        source="test",
        updated_at=updated_at,
        policy=build_test_merge_train_policy(repository=repository),
    )


def build_test_merge_train_policy_with_codex_skills() -> MergeTrainPolicy:
    return parse_merge_train_policy_toml(
        "\n\n".join(
            (
                "schema_version = 1",
                _policy_table("cbusillo/sellyouroutboard"),
                _policy_table("cbusillo/codex-skills"),
            )
        )
    )


def _policy_toml(repository: str) -> str:
    return "\n\n".join(("schema_version = 1", _policy_table(repository)))


def _policy_table(repository: str) -> str:
    return f"""[[policies]]
repository = "{repository}"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"
"""
