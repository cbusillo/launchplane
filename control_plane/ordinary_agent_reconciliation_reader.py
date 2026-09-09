"""Bounded read-only provider evidence for an already-dispatched ordinary effect."""

from urllib.parse import quote
from typing import Never

from pydantic import ValidationError

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.ordinary_agent_effect_lifecycle import ordinary_agent_comment_body
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderEvidenceError,
)


def read_ordinary_effect_observation(
    transport: DeadlineMergeTrainGitHubTransport,
    record: effects.OrdinaryAgentEffectRecord,
) -> effects.OrdinaryAgentProviderObservation:
    try:
        return _read_observation(transport, record)
    except ValidationError as error:
        # Provider-shaped values may fail the typed contract even after their
        # JSON structure is checked. Preserve a bounded incomplete read so an
        # issued custody attempt is never left without an observation.
        raise OrdinaryAgentProviderEvidenceError("effect_observation_incomplete") from error


def _read_observation(
    transport: DeadlineMergeTrainGitHubTransport,
    record: effects.OrdinaryAgentEffectRecord,
) -> effects.OrdinaryAgentProviderObservation:
    command = record.command
    repository = record.target.repository
    prefix = f"/repos/{repository}"
    if command.kind in {"candidate_ref_prepare", "candidate_head_merge", "stack_child_merge"}:
        reference = (
            command.effect.parent_head_ref
            if command.kind == "stack_child_merge"
            else command.effect.candidate_ref
        )
        canonical_ref = reference if reference.startswith("refs/") else "refs/heads/" + reference
        try:
            payload = _object(
                transport.request(
                    method="GET",
                    path=f"{prefix}/git/ref/{quote(canonical_ref.removeprefix('refs/'), safe='/')}",
                )
            )
        except MergeTrainGitHubError as error:
            if error.status_code != 404:
                raise
            return effects.OrdinaryAgentRefObservation(
                repository=repository, ref=reference, sha=None
            )
        if _text(payload.get("ref")) != canonical_ref:
            _deny()
        sha = _text(_object(payload.get("object")).get("sha"))
        proof = _commit(transport, repository, sha, reference)
        if command.kind == "candidate_head_merge" and sha == command.effect.rolling_parent_sha:
            # Both comparison endpoints are immutable object IDs. A later ref
            # movement cannot change what this exact observed commit contains.
            compared = _object(
                transport.request(
                    method="GET",
                    path=f"{prefix}/compare/{quote(command.effect.head_sha, safe='')}...{quote(sha, safe='')}",
                )
            )
            if _text(_object(compared.get("base_commit")).get("sha")) != command.effect.head_sha:
                _deny()
            status = compared.get("status")
            if status not in {"ahead", "behind", "diverged", "identical"}:
                _deny()
            ancestor = _text(_object(compared.get("merge_base_commit")).get("sha"))
            if status in {"ahead", "identical"}:
                if ancestor != command.effect.head_sha:
                    _deny()
                proof = proof.model_copy(update={"contained_head_sha": command.effect.head_sha})
        return proof
    if command.kind in {"pull_request_head_refresh", "pull_request_landing", "stack_child_close"}:
        number = command.effect.pull_request_number
        payload = _object(transport.request(method="GET", path=f"{prefix}/pulls/{number}"))
        base, head = _object(payload.get("base")), _object(payload.get("head"))
        if (
            _positive_integer(payload.get("number")) != number
            or _positive_integer(_object(base.get("repo")).get("id")) != record.target.repository_id
            or _positive_integer(_object(head.get("repo")).get("id")) != record.target.repository_id
            or payload.get("state") not in {"open", "closed"}
            or not isinstance(payload.get("merged"), bool)
        ):
            _deny()
        values: dict[str, object] = {
            "repository": repository,
            "number": number,
            "head_sha": _text(head.get("sha")),
            "base_ref": _text(base.get("ref")),
            "base_sha": _text(base.get("sha")),
            "state": payload["state"],
            "merged": payload["merged"],
        }
        if command.kind == "pull_request_head_refresh":
            proof = _commit(
                transport,
                repository,
                _text(head.get("sha")),
                "refs/heads/" + _text(head.get("ref")),
            )
            values["head_parents"] = proof.parents
        elif command.kind == "pull_request_landing" and payload["merged"]:
            merged_sha = _text(payload.get("merge_commit_sha"))
            proof = _commit(
                transport, repository, merged_sha, "refs/heads/" + _text(base.get("ref"))
            )
            values.update(
                merge_commit_sha=merged_sha,
                merge_commit_tree_sha=proof.tree_sha,
                merge_commit_parents=proof.parents,
            )
        return effects.OrdinaryAgentPullRequestObservation.model_validate(values)
    if command.kind == "stack_child_label":
        label_payload = _list(
            transport.request(
                method="GET",
                path=f"{prefix}/issues/{command.effect.pull_request_number}/labels?per_page=100&page=1",
            )
        )
        labels = tuple(_text(_object(item).get("name")) for item in label_payload)
        present = command.effect.label in labels
        if len(labels) == 100 and not present:
            _deny()
        return effects.OrdinaryAgentLabelObservation(
            repository=repository,
            number=command.effect.pull_request_number,
            label=command.effect.label,
            present=present,
        )
    if command.kind == "stack_child_comment":
        expected_body = ordinary_agent_comment_body(
            body=command.effect.body, effect_id=record.effect_id
        )
        for page in range(1, 4):
            comment_payload = _list(
                transport.request(
                    method="GET",
                    path=f"{prefix}/issues/{command.effect.pull_request_number}/comments?per_page=100&page={page}",
                )
            )
            for raw in comment_payload:
                comment = _object(raw)
                if not isinstance(comment.get("body"), str):
                    _deny()
                if comment["body"] == expected_body:
                    return effects.OrdinaryAgentCommentObservation(
                        repository=repository,
                        number=command.effect.pull_request_number,
                        matching_comment_id=str(_positive_integer(comment.get("id"))),
                        matching_body=expected_body,
                        pages_read=page,
                        exhausted=len(comment_payload) < 100,
                    )
            if len(comment_payload) < 100:
                return effects.OrdinaryAgentCommentObservation(
                    repository=repository,
                    number=command.effect.pull_request_number,
                    pages_read=page,
                    exhausted=True,
                )
        return effects.OrdinaryAgentCommentObservation(
            repository=repository,
            number=command.effect.pull_request_number,
            pages_read=3,
            exhausted=False,
        )
    raise OrdinaryAgentProviderEvidenceError("effect_observation_unsupported")


def _commit(
    transport: DeadlineMergeTrainGitHubTransport, repository: str, sha: str, reference: str
) -> effects.OrdinaryAgentRefObservation:
    payload = _object(
        transport.request(
            method="GET", path=f"/repos/{repository}/git/commits/{quote(sha, safe='')}"
        )
    )
    message = payload.get("message")
    if _text(payload.get("sha")) != sha or not isinstance(message, str):
        _deny()
    parents = payload.get("parents")
    if not isinstance(parents, list):
        _deny()
    return effects.OrdinaryAgentRefObservation(
        repository=repository,
        ref=reference,
        sha=sha,
        tree_sha=_text(_object(payload.get("tree")).get("sha")),
        parents=tuple(_text(_object(parent).get("sha")) for parent in parents),
        commit_message=message,
    )


def _deny() -> Never:
    raise OrdinaryAgentProviderEvidenceError("effect_observation_incomplete")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError("effect_observation_incomplete")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrdinaryAgentProviderEvidenceError("effect_observation_incomplete")
    return value


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrdinaryAgentProviderEvidenceError("effect_observation_incomplete")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > 100:
        raise OrdinaryAgentProviderEvidenceError("effect_observation_incomplete")
    return value
