from __future__ import annotations


ORDINARY_AGENT_GITHUB_APP_INTEGRATION = "ordinary_agent_github_app"
ORDINARY_AGENT_GITHUB_APP_PRIVATE_KEY_BINDING = "private_key"


def ordinary_agent_enrollment_effect_profiles() -> tuple[str, ...]:
    return ("guarded_merge", "head_refresh", "pr_disposition")


def ordinary_agent_enrollment_permissions() -> tuple[str, ...]:
    return ("contents:write", "metadata:read", "pull_requests:write")
