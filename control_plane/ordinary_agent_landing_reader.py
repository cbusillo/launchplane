"""Collect complete landing evidence with one existing custody-scoped transport."""

from __future__ import annotations

from collections.abc import Callable
import json
import time
from urllib.parse import quote

from control_plane.ordinary_agent_repository_roles import OrdinaryRepositoryAdminObservation
from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    read_github_authorship,
    read_github_changed_files,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.change_impact import (
    ChangeImpactBaseEvidence,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTarget,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentLandingPreparation
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentLandingEvidence,
    OrdinaryAgentProviderRequestCounts,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
    require_complete_connection,
)
from control_plane.ordinary_agent_landing_checks import read_landing_checks
from control_plane.ordinary_agent_landing_graphql import (
    LANDING_ENTRY_READ_RESERVE_SECONDS,
    confirm_landing_graphql,
    read_landing_graphql,
)


def read_ordinary_agent_landing_evidence(
    *,
    transport: DeadlineMergeTrainGitHubTransport,
    preparation: OrdinaryAgentLandingPreparation,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
    repository_owner_id: int,
    repository_policy: MergeTrainRepositoryPolicy,
    utc_seconds: Callable[[], float] = time.time,
) -> OrdinaryAgentLandingEvidence:
    """Read under the reserved landing lease; never mint, renew or dispatch here."""
    candidate = candidate_record.candidate
    plan = landing_plan_record.landing_plan
    target = preparation.target
    if (
        preparation.candidate.effect_profile != "merge_train_landing"
        or candidate_record.record_id != preparation.candidate_record_id
        or landing_plan_record.record_id != preparation.landing_plan_record_id
        or plan.landing_plan_sha256 != preparation.landing_plan_sha256
        or candidate.repository != target.repository.lower()
        or candidate.base_branch != target.base_branch
        or plan.candidate_sha != candidate.candidate_sha
        or plan.candidate_sha256 != candidate.candidate_sha256
        or plan.repository != candidate.repository
        or plan.base_branch != candidate.base_branch
        or repository_policy.repository != candidate.repository
        or repository_policy.base_branch != candidate.base_branch
        or any(entry.merge_method != "merge" for entry in plan.entries)
        or tuple((entry.pull_request_number, entry.expected_head_sha) for entry in plan.entries)
        != tuple((entry.pull_request_number, entry.head_sha) for entry in candidate.entries)
        or preparation.entry not in plan.entries
    ):
        raise OrdinaryAgentProviderEvidenceError("landing_read_scope_mismatch")
    terminal = frozenset(
        entry.pull_request_number for entry in plan.entries if entry.status in {"merged", "skipped"}
    )
    observation = read_landing_graphql(
        transport=transport,
        candidate=candidate,
        repository_id=target.repository_id,
        repository_owner_id=repository_owner_id,
        base_sha=preparation.expected_base_sha,
        terminal_entries=terminal,
        utc_seconds=utc_seconds,
    )
    data = json.loads(observation.repository_json)
    base = _object(_object(data["ref"])["target"])
    if _object(base["tree"]).get("oid") != preparation.expected_base_tree_sha:
        raise OrdinaryAgentProviderEvidenceError("landing_base_tree_mismatch")
    technical, protection = read_landing_checks(
        transport=transport,
        observation=observation,
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        base_sha=preparation.expected_base_sha,
        candidate_sha=candidate.candidate_sha,
    )
    owner, name = candidate.repository.split("/", 1)
    repository_path = f"{quote(owner, safe='')}/{quote(name, safe='')}"

    def entry_request(path: str) -> object:
        return transport.request(
            method="GET",
            path=path,
            minimum_remaining_seconds=LANDING_ENTRY_READ_RESERVE_SECONDS,
        )

    admin_observation = OrdinaryRepositoryAdminObservation(
        request=entry_request, repository_path=repository_path
    )
    entries = []
    queue = []
    try:
        for index, entry in enumerate(candidate.entries):
            pr = _object(data[f"pr{index}"])
            author = pr.get("author")
            rest_author = (
                None
                if author is None
                else {
                    "id": _object(author).get("databaseId"),
                    "login": _object(author).get("login"),
                    "type": _object(author).get("__typename"),
                }
            )
            changed_files = read_github_changed_files(
                request=entry_request,
                repository_path=repository_path,
                pull_request_number=entry.pull_request_number,
                max_file_pages=30,
            )
            authorship = read_github_authorship(
                request=entry_request,
                repository_path=repository_path,
                pull_request={"user": rest_author},
                pull_request_number=entry.pull_request_number,
                max_commit_pages=10,
            )
            merge_commit = pr["mergeCommit"]
            entries.append(
                ChangeImpactRepositoryEvidence(
                    target=ChangeImpactTarget(
                        repository_id=str(target.repository_id),
                        repository_owner_id=str(repository_owner_id),
                        repository=candidate.repository,
                        pull_request_number=entry.pull_request_number,
                        head_sha=entry.head_sha,
                        tree_sha=entry.head_tree_sha,
                    ),
                    merge_commit_sha=""
                    if merge_commit is None
                    else _text(_object(merge_commit).get("oid")),
                    changed_files=changed_files,
                    authorship=authorship,
                    base=ChangeImpactBaseEvidence(
                        base_ref=_text(pr.get("baseRefName")),
                        base_sha=_text(pr.get("baseRefOid")),
                    ),
                )
            )
            if entry.pull_request_number not in terminal:
                queue.append(
                    _queue_entry(
                        pr=pr,
                        is_repository_admin=admin_observation.is_admin,
                        repository_policy=repository_policy,
                    )
                )
    except (ChangeImpactRepositoryEvidenceError, ValueError, TypeError) as error:
        raise OrdinaryAgentProviderEvidenceError("landing_entry_evidence_malformed") from error
    confirm_landing_graphql(
        transport=transport,
        candidate=candidate,
        repository_id=target.repository_id,
        repository_owner_id=repository_owner_id,
        base_sha=preparation.expected_base_sha,
        terminal_entries=terminal,
        observation=observation,
    )
    selected = next(
        (
            entry
            for entry in entries
            if entry.target.pull_request_number == preparation.entry.pull_request_number
        ),
        None,
    )
    if selected is None:
        raise OrdinaryAgentProviderEvidenceError("landing_target_missing")
    evidence = OrdinaryAgentLandingEvidence(
        repository_id=target.repository_id,
        repository_owner_id=repository_owner_id,
        repository=candidate.repository,
        base_ref=candidate.base_branch,
        base_identity=OrdinaryAgentCommitIdentity(
            sha=preparation.expected_base_sha,
            tree_sha=preparation.expected_base_tree_sha,
        ),
        repository_evidence=selected,
        candidate_entry_evidence=tuple(entries),
        snapshot=MergeTrainDryRunSnapshot(
            repository=candidate.repository,
            base_branch=candidate.base_branch,
            base_sha=preparation.expected_base_sha,
            pull_requests=tuple(queue),
        ),
        candidate_sha=candidate.candidate_sha,
        technical_checks=technical,
        protection=protection,
        expected_merge_tree_sha=preparation.expected_merge_tree_sha,
        observed_at=observation.observed_at,
        counts=OrdinaryAgentProviderRequestCounts(
            rest_core_requests=transport.rest_core_requests,
            graphql_requests=transport.graphql_requests,
            graphql_points=transport.graphql_points,
        ),
        evidence_sha256="0" * 64,
    )
    return evidence.model_copy(
        update={
            "evidence_sha256": canonical_json_sha256(
                evidence.model_dump(mode="json", exclude={"evidence_sha256"})
            ),
        }
    )


def _queue_entry(
    *,
    pr: dict[str, object],
    is_repository_admin: Callable[[int, str], bool],
    repository_policy: MergeTrainRepositoryPolicy,
) -> MergeTrainPullRequestSnapshot:
    if "author" not in pr:
        raise OrdinaryAgentProviderEvidenceError("landing_queue_actor_missing")
    author = {} if pr["author"] is None else _object(pr["author"])
    raw_actor_id = author.get("databaseId")
    if pr["author"] is not None and (
        isinstance(raw_actor_id, bool) or not isinstance(raw_actor_id, int) or raw_actor_id <= 0
    ):
        raise OrdinaryAgentProviderEvidenceError("landing_queue_actor_missing")
    actor_id = raw_actor_id if isinstance(raw_actor_id, int) else None
    role = (
        "repo_owner"
        if actor_id is not None and pr.get("authorAssociation") == "OWNER"
        else "unknown"
    )
    if (
        actor_id is not None
        and role == "unknown"
        and actor_id not in repository_policy.enqueue.trusted_automation_github_user_ids
    ):
        if is_repository_admin(actor_id, _text(author.get("login"))):
            role = "repo_admin"
    labels = require_complete_connection(pr.get("labels"), label="landing_labels")
    draft = pr.get("isDraft")
    number = pr.get("number")
    if not isinstance(draft, bool):
        raise OrdinaryAgentProviderEvidenceError("landing_queue_draft_missing")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise OrdinaryAgentProviderEvidenceError("landing_queue_number_missing")
    # Landing uses this snapshot to rebuild queue eligibility. Source-head
    # technical checks were not queried and must not inherit combined checks.
    return MergeTrainPullRequestSnapshot(
        number=number,
        url=_text(pr.get("url")),
        title=_text(pr.get("title")),
        state="open",
        is_draft=draft,
        created_at=_text(pr.get("createdAt")),
        labels=tuple(_text(label.get("name")) for label in labels),
        actor_id=actor_id,
        actor_role=role,
        head_sha=_text(pr.get("headRefOid")),
        head_ref=_text(pr.get("headRefName")),
        head_repository=_text(_object(pr.get("headRepository")).get("nameWithOwner")),
        base_sha=_text(pr.get("baseRefOid")),
        base_ref=_text(pr.get("baseRefName")),
        base_repository=_text(_object(pr.get("baseRepository")).get("nameWithOwner")),
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError("landing_entry_evidence_malformed")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrdinaryAgentProviderEvidenceError("landing_entry_evidence_malformed")
    return value
