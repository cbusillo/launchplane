"""Bind source-check projections to the immutable comparison they verified."""

import hashlib
import json
from typing import Literal
from urllib.parse import quote

from control_plane.contracts.advisory_check_projection import (
    CONFIG_AUTHORITY_CHECK_NAME,
    CONFIG_AUTHORITY_CURRENT_CHECK_NAME,
)


def plan_config_authority_transition(
    *,
    base_branch: str,
    required_checks: list[dict[str, object]],
    app_id: int | None,
) -> dict[str, object]:
    """Offline, additive plan from supplied protection evidence; never applies it."""
    if not base_branch.strip() or base_branch != base_branch.strip():
        raise ValueError("An explicit canonical base branch is required.")
    if app_id is not None and (isinstance(app_id, bool) or app_id < 1):
        raise ValueError("The configured checks App must have a positive identifier.")
    retained = []
    for check in required_checks:
        context, publisher = check.get("context"), check.get("app_id")
        if not isinstance(context, str) or not context.strip():
            raise ValueError("Current required-check evidence is malformed.")
        if publisher is not None and (
            not isinstance(publisher, int) or isinstance(publisher, bool) or publisher < -1
        ):
            raise ValueError("Current required-check publisher evidence is malformed.")
        retained.append({"context": context, "app_id": publisher})
    proposed = {
        "context": f"{CONFIG_AUTHORITY_CURRENT_CHECK_NAME}/{quote(base_branch, safe='')}",
        "app_id": app_id,
    }
    return {
        "status": "planned" if app_id is not None else "blocked",
        "mode": "dry_run",
        "base_branch": base_branch,
        "retained_required_checks": retained,
        "proposed_required_check": proposed,
        "next_required_checks": retained
        if app_id is None
        else retained + ([] if proposed in retained else [proposed]),
        "consumer_removals": [],
        "live_policy_applied": False,
        "activation_prerequisites": [
            "deployed_parser_parity",
            "configured_checks_app_identity",
            "current_comparison_missing_and_delayed_refusal",
            "recorded_candidate_push_coverage",
            "retarget_and_stale_context_refusal",
        ],
    }


def config_authority_comparison_id(
    *, repository_id: str, base_sha: object, head_sha: str, event: str, base_branch: object
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "repository_id": repository_id,
                "base_sha": base_sha,
                "event": event,
                "base_branch": base_branch,
                "head_sha": head_sha,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def is_config_authority_check(name: str) -> bool:
    return name.casefold().startswith(CONFIG_AUTHORITY_CHECK_NAME + "/")


def current_config_authority_status(
    *,
    check_runs: list[object],
    repository_id: str,
    base_branch: str,
    base_sha: str,
    head_sha: str,
    app_id: int | None,
) -> Literal["pass", "pending", "fail"]:
    if app_id is None:
        return "pending"
    states = []
    for event in ("pull_request", "push", "merge_group"):
        name = (
            f"{CONFIG_AUTHORITY_CHECK_NAME}/{event.replace('_', '-')}/{quote(base_branch, safe='')}"
        )
        identity = config_authority_comparison_id(
            repository_id=repository_id,
            base_sha=base_sha,
            head_sha=head_sha,
            event=event,
            base_branch=base_branch,
        )
        matches = [
            check
            for check in check_runs
            if isinstance(check, dict)
            and check.get("name")
            in {name, f"{CONFIG_AUTHORITY_CURRENT_CHECK_NAME}/{quote(base_branch, safe='')}"}
            and check.get("head_sha") == head_sha
            and check.get("external_id") == identity
            and isinstance(check.get("app"), dict)
            and check["app"].get("id") == app_id
        ]
        if not matches:
            continue
        latest = max(matches, key=lambda check: int(check.get("id") or 0))
        if latest.get("status") != "completed":
            states.append("pending")
        elif latest.get("conclusion") == "success":
            states.append("pass")
        elif latest.get("conclusion") in {"failure", "action_required", "timed_out", "cancelled"}:
            states.append("fail")
        else:
            states.append("pending")
    if "fail" in states:
        return "fail"
    if "pass" in states and "pending" not in states:
        return "pass"
    return "pending"
