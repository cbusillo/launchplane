from control_plane.service_auth import (
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
)


class _StubVerifier:
    def __init__(self, identity: GitHubActionsIdentity):
        self.identity = identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        if token != "valid-token":
            raise ValueError("OIDC bearer token is required.")
        return self.identity


def _identity(
    *,
    repository: str = "every/verireel",
    workflow_ref: str = "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
    job_workflow_ref: str = "",
    event_name: str = "pull_request",
    ref: str = "refs/heads/main",
    environment: str = "",
) -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner="every",
        workflow_ref=workflow_ref,
        job_workflow_ref=job_workflow_ref,
        ref=ref,
        ref_type="branch",
        event_name=event_name,
        environment=environment,
        subject="repo:every/verireel:pull_request",
        sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        raw_claims={
            "repository": repository,
            "workflow_ref": workflow_ref,
            "run_id": "1001",
            "run_attempt": "1",
        },
    )


def _local_operator_policy(
    *,
    actions: tuple[str, ...],
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
    subject: str = "local-owner-agent",
    token_label: str = "local-owner-write",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        local_operators=(
            LocalOperatorPolicyRule(
                subjects=(subject,),
                token_labels=(token_label,),
                products=products,
                contexts=contexts,
                actions=actions,
            ),
        )
    )
