"""Concrete bounded provider observations for the ordinary shared controller."""

import json
import time
from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    MAX_ORDINARY_LANDING_ENTRIES,
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentPullRequestHeadIdentity,
    OrdinaryAgentReadmissionObservation,
    OrdinaryAgentReadmissionPullRequestObservation,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
    require_complete_graphql_data,
)
from control_plane.tenant_admission_controller import _required_technical_check_state
from control_plane.ordinary_agent_landing_checks import evaluate_observed_commit_checks
from control_plane.ordinary_agent_landing_graphql import (
    OrdinaryLandingGraphQLObservation,
    _CHECKS,
    _IDENTITY_FIELDS,
    _PR_DETAILS,
    _PROTECTION,
)
from control_plane.ordinary_agent_landing_reader import _queue_entry
from control_plane.ordinary_agent_repository_roles import OrdinaryRepositoryAdminObservation


def read_ordinary_controller_snapshot(
    *,
    transport: DeadlineMergeTrainGitHubTransport,
    request: OrdinaryAgentFiniteRequestRecord,
    repository_owner_id: int,
    repository_policy: MergeTrainRepositoryPolicy,
    utc_seconds: Callable[[], float] = time.time,
) -> OrdinaryAgentMergeTrainSnapshotResult | OrdinaryAgentReadmissionObservation:
    target = request.target
    if not 1 <= len(request.pull_requests) <= MAX_ORDINARY_LANDING_ENTRIES:
        raise OrdinaryAgentProviderEvidenceError("snapshot_entry_limit")
    if (repository_policy.repository, repository_policy.base_branch) != (
        target.repository,
        target.base_branch,
    ):
        raise OrdinaryAgentProviderEvidenceError("snapshot_policy_target_mismatch")
    owner, name = target.repository.split("/", 1)
    variables: dict[str, object] = {
        "owner": owner,
        "name": name,
        "base": f"refs/heads/{target.base_branch}",
    }
    declarations = ["$owner:String!", "$name:String!", "$base:String!"]
    selections = []
    comparisons = []
    for index, entry in enumerate(request.pull_requests):
        declarations.extend(
            (f"$number{index}:Int!", f"$head{index}:GitObjectID!", f"$headRef{index}:String!")
        )
        variables.update(
            {
                f"number{index}": entry.number,
                f"head{index}": entry.head_sha,
                f"headRef{index}": entry.head_sha,
            }
        )
        selections.append(
            f"pr{index}:pullRequest(number:$number{index}){{{_IDENTITY_FIELDS} {_PR_DETAILS} mergeable}} "
            f"head{index}:object(oid:$head{index}){{... on Commit{{oid tree{{oid}} {_CHECKS}}}}}"
        )
        comparisons.append(
            f"compare{index}:compare(headRef:$headRef{index}){{status baseTarget{{oid}} headTarget{{oid}}}}"
        )
    query = (
        f"query({','.join(declarations)}){{rateLimit{{cost}} repository(owner:$owner,name:$name){{"
        "databaseId nameWithOwner owner{... on User{databaseId} ... on Organization{databaseId}} "
        f"ref(qualifiedName:$base){{name target{{... on Commit{{oid tree{{oid}}}}}} {_PROTECTION} {' '.join(comparisons)}}} "
        + " ".join(selections)
        + "}}"
    )
    observed_at = int(utc_seconds())
    data = require_complete_graphql_data(
        transport.request(
            method="POST", path="/graphql", body={"query": query, "variables": variables}
        ),
        transport=transport,
    )
    repository = _object(data.get("repository"))
    _require_repository(repository, request, repository_owner_id)
    ref = _object(repository.get("ref"))
    base_identity = _identity(ref.get("target"))
    _commit_sha(base_identity.sha)
    _commit_sha(base_identity.tree_sha)
    if ref.get("name") != target.base_branch:
        raise OrdinaryAgentProviderEvidenceError("snapshot_base_ref_mismatch")
    pull_request_data: list[dict[str, object]] = []
    observed_pull_requests: list[OrdinaryAgentReadmissionPullRequestObservation] = []
    for index, expected in enumerate(request.pull_requests):
        pr = _object(repository.get(f"pr{index}"))
        if pr.get("number") != expected.number:
            raise OrdinaryAgentProviderEvidenceError("snapshot_pull_request_mismatch")
        lifecycle = _pull_request_lifecycle(pr.get("state"))
        if pr.get("baseRefName") != target.base_branch:
            raise OrdinaryAgentProviderEvidenceError("snapshot_base_ref_mismatch")
        repositories = []
        for field in ("headRepository", "baseRepository"):
            candidate_repository = _object(pr.get(field))
            repository_id = _positive_id(candidate_repository.get("databaseId"))
            repository_name = _text(candidate_repository.get("nameWithOwner"))
            if repository_id != target.repository_id or repository_name != target.repository:
                raise OrdinaryAgentProviderEvidenceError("snapshot_repository_mismatch")
            repositories.append((repository_id, repository_name))
        if lifecycle == "open" and _object(pr.get("headRef")).get("name") != pr.get("headRefName"):
            raise OrdinaryAgentProviderEvidenceError("snapshot_head_ref_missing")
        observed_pull_requests.append(
            OrdinaryAgentReadmissionPullRequestObservation(
                number=expected.number,
                head_sha=_commit_sha(pr.get("headRefOid")),
                base_ref=target.base_branch,
                lifecycle=lifecycle,
                head_repository_id=repositories[0][0],
                head_repository=repositories[0][1],
                base_repository_id=repositories[1][0],
                base_repository=repositories[1][1],
            )
        )
        pull_request_data.append(pr)
    base_changed = base_identity.sha != request.base_sha
    head_changed = any(
        observed.head_sha != captured.head_sha
        for captured, observed in zip(request.pull_requests, observed_pull_requests, strict=True)
    )
    if base_changed or head_changed:
        return _readmission_observation(
            request=request,
            repository_owner_id=repository_owner_id,
            observed_at=observed_at,
            base_identity=base_identity,
            pull_requests=tuple(observed_pull_requests),
            counts=_counts(transport),
        )
    repository_path = f"{quote(owner, safe='')}/{quote(name, safe='')}"
    rules = _rules(transport, repository_path, target.base_branch)
    admins = OrdinaryRepositoryAdminObservation(
        request=lambda path: transport.request(method="GET", path=path),
        repository_path=repository_path,
    )
    pull_requests = []
    heads = []
    protection = None
    for index, expected in enumerate(request.pull_requests):
        pr = pull_request_data[index]
        head = _object(repository.get(f"head{index}"))
        identity = _identity(head)
        if identity.sha != expected.head_sha:
            raise OrdinaryAgentProviderEvidenceError("snapshot_head_changed")
        state, mergeable = pr.get("state"), pr.get("mergeable")
        if mergeable not in {
            "MERGEABLE",
            "CONFLICTING",
            "UNKNOWN",
        }:
            raise OrdinaryAgentProviderEvidenceError("snapshot_pr_state_missing")
        checks, protection = evaluate_observed_commit_checks(
            observation=_observation(
                {"ref": {**ref, "compare": ref.get(f"compare{index}")}, "candidate": head},
                observed_at,
            ),
            rules=rules,
            base_branch=target.base_branch,
            base_sha=request.base_sha,
            candidate_sha=expected.head_sha,
        )
        # The shared queue treats failed checks and a stale base separately.
        # Retain strict freshness in branch_update_required so clean checks can
        # request a refresh instead of being mislabeled as failing CI.
        source_status = _required_technical_check_state(
            required_checks=checks.required_checks,
            signals=checks.signals,
            strict=False,
            base_up_to_date=None,
        )
        queue = _queue_entry(
            pr=pr, is_repository_admin=admins.is_admin, repository_policy=repository_policy
        )
        pull_requests.append(
            MergeTrainPullRequestSnapshot.model_validate(
                {
                    **queue.model_dump(),
                    "state": str(state).lower(),
                    "mergeable": str(mergeable).lower(),
                    "required_checks_status": "unknown"
                    if source_status == "unavailable"
                    else source_status,
                    "branch_update_required": checks.strict and checks.base_up_to_date is False,
                }
            )
        )
        heads.append(
            OrdinaryAgentPullRequestHeadIdentity(
                pull_request_number=expected.number, identity=identity
            )
        )
    assert protection is not None
    result = OrdinaryAgentMergeTrainSnapshotResult(
        snapshot=MergeTrainDryRunSnapshot(
            repository=target.repository,
            base_branch=target.base_branch,
            base_sha=request.base_sha,
            pull_requests=tuple(pull_requests),
        ),
        base_identity=base_identity,
        head_identities=tuple(heads),
        protection=protection,
        counts=_counts(transport),
        snapshot_sha256="0" * 64,
    )
    return result.model_copy(
        update={
            "snapshot_sha256": canonical_json_sha256(
                result.model_dump(mode="json", exclude={"snapshot_sha256"})
            )
        }
    )


def read_ordinary_candidate_check(
    *,
    transport: DeadlineMergeTrainGitHubTransport,
    request: OrdinaryAgentFiniteRequestRecord,
    repository_owner_id: int,
    candidate_sha: str,
    utc_seconds: Callable[[], float] = time.time,
) -> OrdinaryAgentCandidateCheckResult:
    target = request.target
    owner, name = target.repository.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$base:String!,$candidate:GitObjectID!,$candidateRef:String!){rateLimit{cost} "
        "repository(owner:$owner,name:$name){databaseId nameWithOwner owner{... on User{databaseId} ... on Organization{databaseId}} "
        f"ref(qualifiedName:$base){{name target{{... on Commit{{oid tree{{oid}}}}}} {_PROTECTION} compare(headRef:$candidateRef){{status baseTarget{{oid}} headTarget{{oid}}}}}} "
        f"candidate:object(oid:$candidate){{... on Commit{{oid tree{{oid}} {_CHECKS}}}}}}}}}"
    )
    observed_at = int(utc_seconds())
    data = require_complete_graphql_data(
        transport.request(
            method="POST",
            path="/graphql",
            body={
                "query": query,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "base": f"refs/heads/{target.base_branch}",
                    "candidate": candidate_sha,
                    "candidateRef": candidate_sha,
                },
            },
        ),
        transport=transport,
    )
    repository = _object(data.get("repository"))
    _require_repository(repository, request, repository_owner_id)
    if _identity(_object(repository.get("ref")).get("target")).sha != request.base_sha:
        raise OrdinaryAgentProviderEvidenceError("snapshot_base_changed")
    candidate = _identity(repository.get("candidate"))
    if candidate.sha != candidate_sha:
        raise OrdinaryAgentProviderEvidenceError("snapshot_candidate_changed")
    checks, protection = evaluate_observed_commit_checks(
        observation=_observation(repository, observed_at),
        rules=_rules(
            transport, f"{quote(owner, safe='')}/{quote(name, safe='')}", target.base_branch
        ),
        base_branch=target.base_branch,
        base_sha=request.base_sha,
        candidate_sha=candidate_sha,
    )
    result = OrdinaryAgentCandidateCheckResult(
        candidate_identity=candidate,
        protection=protection,
        status="unknown" if checks.status == "unavailable" else checks.status,
        counts=_counts(transport),
        observation_sha256="0" * 64,
    )
    return result.model_copy(
        update={
            "observation_sha256": canonical_json_sha256(
                result.model_dump(mode="json", exclude={"observation_sha256"})
            )
        }
    )


def _rules(
    transport: DeadlineMergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> object:
    return transport.request(
        method="GET",
        path=f"/repos/{repository_path}/rules/branches/{quote(base_branch, safe='')}?per_page=100",
    )


def _require_repository(
    data: dict[str, object], request: OrdinaryAgentFiniteRequestRecord, owner_id: int
) -> None:
    if (
        _positive_id(data.get("databaseId")) != request.target.repository_id
        or data.get("nameWithOwner") != request.target.repository
        or _positive_id(_object(data.get("owner")).get("databaseId")) != owner_id
    ):
        raise OrdinaryAgentProviderEvidenceError("snapshot_repository_mismatch")


def _identity(value: object) -> OrdinaryAgentCommitIdentity:
    data = _object(value)
    return OrdinaryAgentCommitIdentity(
        sha=_text(data.get("oid")), tree_sha=_text(_object(data.get("tree")).get("oid"))
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError("snapshot_payload_malformed")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrdinaryAgentProviderEvidenceError("snapshot_payload_malformed")
    return value


def _observation(data: dict[str, object], observed_at: int) -> OrdinaryLandingGraphQLObservation:
    return OrdinaryLandingGraphQLObservation(
        observed_at=observed_at,
        repository_json=json.dumps(data),
        identity_sha256=canonical_json_sha256(data),
    )


def _counts(transport: DeadlineMergeTrainGitHubTransport) -> OrdinaryAgentProviderRequestCounts:
    return OrdinaryAgentProviderRequestCounts(
        rest_core_requests=transport.rest_core_requests,
        graphql_requests=transport.graphql_requests,
        graphql_points=transport.graphql_points,
    )


def _readmission_observation(
    *,
    request: OrdinaryAgentFiniteRequestRecord,
    repository_owner_id: int,
    observed_at: int,
    base_identity: OrdinaryAgentCommitIdentity,
    pull_requests: tuple[OrdinaryAgentReadmissionPullRequestObservation, ...],
    counts: OrdinaryAgentProviderRequestCounts,
) -> OrdinaryAgentReadmissionObservation:
    values: dict[str, object] = {
        "schema_version": 1,
        "observed_at": observed_at,
        "target": request.target.model_dump(mode="json"),
        "repository_owner_id": repository_owner_id,
        "captured_base_sha": request.base_sha,
        "captured_pull_requests": [item.model_dump(mode="json") for item in request.pull_requests],
        "base_identity": base_identity.model_dump(mode="json"),
        "pull_requests": [item.model_dump(mode="json") for item in pull_requests],
        "drift": (
            "base_and_head"
            if base_identity.sha != request.base_sha
            and any(
                observed.head_sha != captured.head_sha
                for captured, observed in zip(request.pull_requests, pull_requests, strict=True)
            )
            else "base"
            if base_identity.sha != request.base_sha
            else "head"
        ),
        "counts": counts.model_dump(mode="json"),
    }
    values["observation_sha256"] = canonical_json_sha256(values)
    return OrdinaryAgentReadmissionObservation.model_validate(values)


def _commit_sha(value: object) -> str:
    sha = _text(value)
    if len(sha) not in {40, 64} or any(character not in "0123456789abcdef" for character in sha):
        raise OrdinaryAgentProviderEvidenceError("snapshot_identity_missing")
    return sha


def _pull_request_lifecycle(value: object) -> Literal["open", "closed", "merged"]:
    if value == "OPEN":
        return "open"
    if value == "CLOSED":
        return "closed"
    if value == "MERGED":
        return "merged"
    raise OrdinaryAgentProviderEvidenceError("snapshot_pr_state_missing")


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrdinaryAgentProviderEvidenceError("snapshot_identity_missing")
    return value
