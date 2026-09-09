"""Batched landing identity reads under one existing provider deadline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import time

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.ordinary_agent_snapshot import MAX_ORDINARY_LANDING_ENTRIES
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
    require_complete_graphql_data,
)


LANDING_ENTRY_READ_RESERVE_SECONDS = 61
LANDING_CONFIRMATION_RESERVE_SECONDS = 46

# The same selection is used at both ends of acquisition. Terminal predecessor
# branches may be deleted; their immutable head commit must still be readable.
_IDENTITY_FIELDS = """
number headRefOid baseRefOid baseRefName updatedAt state
mergeCommit { oid }
headRef { name }
headRepository { databaseId nameWithOwner }
baseRepository { databaseId nameWithOwner }
"""
_PR_DETAILS = """
url title createdAt isDraft headRefName authorAssociation
labels(first: 100) { totalCount pageInfo { hasNextPage } nodes { name } }
author { __typename login ... on User { databaseId } ... on Bot { databaseId } }
"""
_PROTECTION = """
branchProtectionRule {
  requiresStatusChecks requiresStrictStatusChecks
  requiredStatusChecks { context app { databaseId } }
}
"""
_CHECKS = """
statusCheckRollup { contexts(first: 100) {
  totalCount pageInfo { hasNextPage }
  nodes { __typename
    ... on CheckRun { name status conclusion checkSuite { app { databaseId } } }
    ... on StatusContext { context state }
  }
} }
"""


@dataclass(frozen=True)
class OrdinaryLandingGraphQLObservation:
    observed_at: int
    repository_json: str
    identity_sha256: str


def read_landing_graphql(
    *,
    transport: DeadlineMergeTrainGitHubTransport,
    candidate: MergeTrainBatchCandidate,
    repository_id: int,
    repository_owner_id: int,
    base_sha: str,
    terminal_entries: frozenset[int],
    utc_seconds: Callable[[], float] = time.time,
) -> OrdinaryLandingGraphQLObservation:
    """Read all recorded entries and one combined candidate check rollup."""
    body = _query(candidate, detailed=True)
    observed_at = int(utc_seconds())
    data = require_complete_graphql_data(
        transport.request(
            method="POST",
            path="/graphql",
            body=body,
            minimum_remaining_seconds=LANDING_ENTRY_READ_RESERVE_SECONDS,
        ),
        transport=transport,
    )
    repository = _object(data.get("repository"))
    signature = _identity_signature(
        repository,
        candidate=candidate,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        base_sha=base_sha,
        terminal_entries=terminal_entries,
    )
    return OrdinaryLandingGraphQLObservation(
        observed_at=observed_at,
        repository_json=json.dumps(repository, sort_keys=True, separators=(",", ":")),
        identity_sha256=signature,
    )


def confirm_landing_graphql(
    *,
    transport: DeadlineMergeTrainGitHubTransport,
    candidate: MergeTrainBatchCandidate,
    repository_id: int,
    repository_owner_id: int,
    base_sha: str,
    terminal_entries: frozenset[int],
    observation: OrdinaryLandingGraphQLObservation,
) -> None:
    """Reject drift without renewing the observation or acquiring a new lease."""
    data = require_complete_graphql_data(
        transport.request(
            method="POST",
            path="/graphql",
            body=_query(candidate, detailed=False),
            minimum_remaining_seconds=LANDING_CONFIRMATION_RESERVE_SECONDS,
        ),
        transport=transport,
    )
    signature = _identity_signature(
        _object(data.get("repository")),
        candidate=candidate,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        base_sha=base_sha,
        terminal_entries=terminal_entries,
    )
    if signature != observation.identity_sha256:
        raise OrdinaryAgentProviderEvidenceError("landing_identity_changed")


def _query(candidate: MergeTrainBatchCandidate, *, detailed: bool) -> dict[str, object]:
    if not 1 <= len(candidate.entries) <= MAX_ORDINARY_LANDING_ENTRIES:
        raise OrdinaryAgentProviderEvidenceError("landing_entry_limit")
    owner, name = candidate.repository.split("/", 1)
    variables: dict[str, object] = {
        "owner": owner,
        "name": name,
        "base": f"refs/heads/{candidate.base_branch}",
        "candidate": candidate.candidate_sha,
    }
    declarations = [
        "$owner: String!",
        "$name: String!",
        "$base: String!",
        "$candidate: GitObjectID!",
    ]
    selections = []
    for index, entry in enumerate(candidate.entries):
        declarations.extend((f"$number{index}: Int!", f"$head{index}: GitObjectID!"))
        variables[f"number{index}"] = entry.pull_request_number
        variables[f"head{index}"] = entry.head_sha
        selections.append(
            f"pr{index}: pullRequest(number: $number{index}) {{ {_IDENTITY_FIELDS} "
            f"{_PR_DETAILS if detailed else ''} }} "
            f"head{index}: object(oid: $head{index}) {{ ... on Commit {{ oid tree {{ oid }} }} }}"
        )
    comparison = ""
    if detailed:
        declarations.append("$candidateRef: String!")
        variables["candidateRef"] = candidate.candidate_ref
        comparison = (
            "compare(headRef: $candidateRef) { status baseTarget { oid } headTarget { oid } }"
        )
    query = (
        f"query({', '.join(declarations)}) {{ rateLimit {{ cost }} "
        "repository(owner: $owner, name: $name) { databaseId nameWithOwner "
        "owner { ... on User { databaseId } ... on Organization { databaseId } } "
        "ref(qualifiedName: $base) { name target { ... on Commit { oid tree { oid } } } "
        f"{_PROTECTION if detailed else ''} {comparison} }} "
        "candidate: object(oid: $candidate) { ... on Commit { oid tree { oid } "
        f"{_CHECKS if detailed else ''} }} }} " + " ".join(selections) + " } }"
    )
    return {"query": query, "variables": variables}


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError("landing_identity_missing")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrdinaryAgentProviderEvidenceError("landing_identity_missing")
    return value


def _identity_signature(
    repository: dict[str, object],
    *,
    candidate: MergeTrainBatchCandidate,
    repository_id: int,
    repository_owner_id: int,
    base_sha: str,
    terminal_entries: frozenset[int],
) -> str:
    numbers = {entry.pull_request_number for entry in candidate.entries}
    if len(numbers) != len(candidate.entries) or not terminal_entries <= numbers:
        raise OrdinaryAgentProviderEvidenceError("landing_entry_identity_mismatch")
    if (
        repository.get("databaseId") != repository_id
        or _text(repository.get("nameWithOwner")).lower() != candidate.repository
        or _object(repository.get("owner")).get("databaseId") != repository_owner_id
        or _object(repository.get("ref")).get("name") != candidate.base_branch
        or _object(_object(repository.get("ref")).get("target")).get("oid") != base_sha
        or _object(repository.get("candidate")).get("oid") != candidate.candidate_sha
        or _object(_object(repository.get("candidate")).get("tree")).get("oid")
        != candidate.candidate_tree_sha
    ):
        raise OrdinaryAgentProviderEvidenceError("landing_repository_identity_mismatch")
    _text(_object(_object(_object(repository["ref"])["target"]).get("tree")).get("oid"))
    identities: list[object] = [
        repository["databaseId"],
        repository["nameWithOwner"],
        repository["owner"],
        _object(repository["ref"])["target"],
        _object(repository["candidate"])["oid"],
    ]
    for index, entry in enumerate(candidate.entries):
        pull_request = _object(repository.get(f"pr{index}"))
        head = _object(repository.get(f"head{index}"))
        if (
            pull_request.get("number") != entry.pull_request_number
            or pull_request.get("headRefOid") != entry.head_sha
            or pull_request.get("baseRefName") != candidate.base_branch
            or head.get("oid") != entry.head_sha
            or _object(head.get("tree")).get("oid") != entry.head_tree_sha
            or not _text(pull_request.get("baseRefOid"))
            or not _text(pull_request.get("updatedAt"))
            or "mergeCommit" not in pull_request
            or "headRef" not in pull_request
            or "mergeCommit" not in pull_request
            or pull_request.get("state") not in {"OPEN", "CLOSED", "MERGED"}
            or (
                entry.pull_request_number not in terminal_entries
                and (pull_request.get("state") != "OPEN" or not pull_request.get("headRef"))
            )
        ):
            raise OrdinaryAgentProviderEvidenceError("landing_entry_identity_mismatch")
        if pull_request.get("mergeCommit") is not None:
            _text(_object(pull_request["mergeCommit"]).get("oid"))
        elif pull_request.get("state") == "MERGED":
            raise OrdinaryAgentProviderEvidenceError("landing_identity_missing")
        for side in ("headRepository", "baseRepository"):
            # The ordinary finite-job path supports same-repository heads only.
            identity = _object(pull_request.get(side))
            if (
                identity.get("databaseId") != repository_id
                or _text(identity.get("nameWithOwner")).lower() != candidate.repository
            ):
                raise OrdinaryAgentProviderEvidenceError("landing_repository_identity_mismatch")
        identities.append(
            {
                key: pull_request.get(key)
                for key in (
                    "number",
                    "headRefOid",
                    "baseRefOid",
                    "baseRefName",
                    "updatedAt",
                    "state",
                    "mergeCommit",
                    "headRef",
                    "headRepository",
                    "baseRepository",
                )
            }
        )
    return hashlib.sha256(
        json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
