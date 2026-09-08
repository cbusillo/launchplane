"""Pure terminal snapshot decisions; no provider call, grant, or execution.

A replay retains its original open observation and never proves current openness.
Only the locked storage method may mint a v2 intent from current policy evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from control_plane.contracts.every_code_feedback_resume import (
    EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION,
    EveryCodeFeedbackAcceptanceRecord,
    EveryCodeFeedbackPolicyDecisionProvenance,
    EveryCodeFeedbackPullRequestOpenObservation,
    EveryCodeFeedbackResumeIntentRecord,
    parse_every_code_feedback_timestamp,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord

# Bounded snapshots, not a guarantee that GitHub cannot close after the read.
# Adapter clock skew means the worst real age can reach 35 seconds.
EVERY_CODE_FEEDBACK_OPEN_MAX_AGE = timedelta(seconds=30)
EVERY_CODE_FEEDBACK_OPEN_FUTURE_SKEW = timedelta(seconds=5)

EveryCodeFeedbackIntentDecisionStatus = Literal[
    "mint",
    "replay",
    "request_not_terminal",
    "acceptance_expired",
    "acceptance_superseded",
    "pull_request_closed",
    "pull_request_state_unknown",
    "pull_request_observation_stale",
    "binding_mismatch",
    "authority_denied",
    "terminal_snapshot_mismatch",
    "legacy_unverified",
    "missing",
    "contention",
    "clock_anomaly",
]


@dataclass(frozen=True)
class EveryCodeFeedbackIntentMintResult:
    status: EveryCodeFeedbackIntentDecisionStatus
    record: EveryCodeFeedbackResumeIntentRecord | None = None


def decide_every_code_feedback_resume_intent(
    *,
    acceptance: EveryCodeFeedbackAcceptanceRecord,
    request: EveryCodeWorkRequestRecord,
    current_acceptance: EveryCodeFeedbackAcceptanceRecord,
    closure_present: bool,
    open_observation: EveryCodeFeedbackPullRequestOpenObservation | None,
    current_policy_provenance: EveryCodeFeedbackPolicyDecisionProvenance | None,
    database_now: str,
    existing_intent: EveryCodeFeedbackResumeIntentRecord | None,
) -> EveryCodeFeedbackIntentMintResult:
    revision = acceptance.revision
    if (
        request.request_id != acceptance.request_id
        or request.repository != revision.repository
        or request.issue_number != acceptance.issue_number
        or request.issue_url != acceptance.issue_url
        or request.result_pr_url != acceptance.retained_pull_request_url
    ):
        return EveryCodeFeedbackIntentMintResult("binding_mismatch")
    if current_acceptance.acceptance_digest != acceptance.acceptance_digest:
        return EveryCodeFeedbackIntentMintResult("acceptance_superseded")
    now = parse_every_code_feedback_timestamp(database_now)
    if now >= parse_every_code_feedback_timestamp(acceptance.eligible_until):
        return EveryCodeFeedbackIntentMintResult("acceptance_expired")
    if now < parse_every_code_feedback_timestamp(acceptance.created_at):
        return EveryCodeFeedbackIntentMintResult("clock_anomaly")
    if request.state not in {"done", "blocked"} or not request.claimed_by_host:
        return EveryCodeFeedbackIntentMintResult("request_not_terminal")
    if closure_present:
        # A recorded close terminally invalidates this acceptance. Reopening
        # cannot resurrect it; future ingestion must create fresh evidence.
        return EveryCodeFeedbackIntentMintResult("pull_request_closed")
    if (
        current_policy_provenance is None
        or current_policy_provenance.action != EVERY_CODE_FEEDBACK_RESUME_REQUEST_ACTION
        or current_policy_provenance.instance != f"github-repository:{revision.repository_id}"
    ):
        return EveryCodeFeedbackIntentMintResult("authority_denied")
    if existing_intent is not None:
        if (
            existing_intent.acceptance_id != acceptance.acceptance_id
            or existing_intent.acceptance_digest != acceptance.acceptance_digest
            or existing_intent.expected_lifecycle_id != request.lifecycle_id
            or existing_intent.expected_fencing_token != request.fencing_token
            or existing_intent.expected_terminal_state != request.state
            or existing_intent.request_id != request.request_id
            or existing_intent.retained_host != request.claimed_by_host
            or existing_intent.retained_pull_request_url != request.result_pr_url
            or existing_intent.eligible_until != acceptance.eligible_until
        ):
            return EveryCodeFeedbackIntentMintResult("terminal_snapshot_mismatch")
        if existing_intent.schema_version != 2:
            return EveryCodeFeedbackIntentMintResult("legacy_unverified")
        return EveryCodeFeedbackIntentMintResult("replay", existing_intent)
    if open_observation is None:
        return EveryCodeFeedbackIntentMintResult("pull_request_state_unknown")
    if (
        open_observation.repository_id != revision.repository_id
        or open_observation.repository_owner_id != revision.repository_owner_id
        or open_observation.pull_request_number != revision.pull_request_number
        or open_observation.pull_request_node_id != revision.pull_request_node_id
    ):
        return EveryCodeFeedbackIntentMintResult("binding_mismatch")
    age = now - parse_every_code_feedback_timestamp(open_observation.observed_at)
    if not -EVERY_CODE_FEEDBACK_OPEN_FUTURE_SKEW <= age <= EVERY_CODE_FEEDBACK_OPEN_MAX_AGE:
        return EveryCodeFeedbackIntentMintResult("pull_request_observation_stale")
    return EveryCodeFeedbackIntentMintResult("mint")
