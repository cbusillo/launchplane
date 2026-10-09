"""Interpret bounded quota metadata on durable controller failure records."""

import re
from datetime import datetime, timedelta, timezone
from typing import Protocol

from control_plane.contracts.merge_train_admission import MergeTrainAdmissionDecision
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainRepositoryPolicy,
)


class ControllerQuotaReadStore(Protocol):
    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]: ...


def defer_for_recorded_github_quota(
    *,
    decision: MergeTrainAdmissionDecision,
    controller_state: MergeTrainControllerStateRecord | None,
    store: ControllerQuotaReadStore,
    policy_record: MergeTrainPolicyRecord | None,
) -> MergeTrainAdmissionDecision:
    states = [controller_state] if controller_state is not None else []
    if policy_record is not None:
        target = policy_record.policy.find_repository_policy(
            repository=decision.repository, base_branch=decision.base_branch
        )
        scope = _installation_scope(target)
        if scope is not None:
            for policy in policy_record.policy.policies:
                if _installation_scope(policy) != scope or (
                    policy.repository,
                    policy.base_branch,
                ) == (target.repository, target.base_branch):
                    continue
                records = store.list_merge_train_controller_state_records(
                    repository=policy.repository, base_branch=policy.base_branch, limit=1
                )
                if records:
                    state = records[0]
                    # The current source cannot attest a failure under an older binding.
                    if (
                        state.repository == policy.repository.casefold()
                        and state.base_branch == policy.base_branch
                        and state.policy_key == policy.policy_key
                        and state.policy_sha256 == policy_record.policy_sha256
                    ):
                        states.append(state)
    deadlines = [deadline for state in states if (deadline := _retry_deadline(state)) is not None]
    if not deadlines:
        return decision
    deadline = max(deadlines)
    if _timestamp(decision.requested_at) >= deadline:
        return decision
    if decision.status == "deferred" and _timestamp(decision.next_allowed_at) >= deadline:
        return decision
    next_allowed = max(deadline, _timestamp(decision.next_allowed_at))
    return decision.model_copy(
        update={
            "status": "deferred",
            "reason_code": "github_rate_limit_pending",
            "next_allowed_at": next_allowed.isoformat().replace("+00:00", "Z"),
            "detail": "Recorded GitHub quota evidence defers this train before provider reads; "
            "normal admission and controller reconciliation resume after the deadline.",
        }
    )


def _installation_scope(policy: MergeTrainRepositoryPolicy) -> tuple[int, str] | None:
    app = policy.github_token.github_app
    if app is None:
        return None
    # Repository-restricted tokens still consume the App installation's quota.
    # GitHub installs an App on an account; a different App or account is independent.
    return app.app_id, policy.repository.split("/", 1)[0].casefold()


def _retry_deadline(state: MergeTrainControllerStateRecord) -> datetime | None:
    parts = state.reconciliation_detail.split("; ")
    if state.status != "reconcile_required" or parts[0] != "retryable:github_rate_limited":
        return None
    observed_at = _timestamp(state.updated_at)
    deadlines: list[datetime] = []
    for part in parts[1:]:
        match = re.fullmatch(r"(reset_at|retry_after_seconds):([0-9]{1,12})", part)
        if match is None:
            continue
        if match[1] == "reset_at" and "primary_exhausted:false" in parts:
            # A secondary refusal can carry a primary reset for quota still available.
            continue
        try:
            value = int(match[2])
            deadline = (
                datetime.fromtimestamp(value, tz=timezone.utc)
                if match[1] == "reset_at"
                else observed_at + timedelta(seconds=value)
            )
        except (OverflowError, ValueError, OSError):
            continue
        deadlines.append(deadline)
    # Quota refusals without usable headers must not retry on every wake.
    return max(deadlines) if deadlines else observed_at + timedelta(seconds=60)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
