from __future__ import annotations


def ordinary_agent_enrollment_effect_profiles() -> tuple[str, ...]:
    return (
        "guarded_merge",
        "head_refresh",
        "merge_train_landing",
        "merge_train_snapshot",
        "pr_disposition",
    )


def ordinary_agent_enrollment_permissions() -> tuple[str, ...]:
    return (
        "administration:read",
        "checks:read",
        "contents:write",
        "metadata:read",
        "pull_requests:write",
        "statuses:read",
    )
