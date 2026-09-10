"""One immediate provider merge from a newly finalized landing admission."""

from __future__ import annotations

from collections.abc import Callable
import time

from control_plane.contracts.ordinary_agent_effect import (
    LANDING_EVIDENCE_MAX_AGE_SECONDS,
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentEffectStore,
    OrdinaryAgentKnownNotDispatchedOutcome,
    OrdinaryAgentLandingFinalization,
    OrdinaryAgentRefObservation,
    OrdinaryAgentUnknownOutcome,
)
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
    require_complete_connection,
    require_complete_graphql_data,
)


class OrdinaryLandingDispatchStopped(RuntimeError):
    pass


class FinalizedOrdinaryLandingDispatcher:
    """Consume the created result once, using its still-open original lease."""

    def __init__(
        self,
        *,
        finalization: OrdinaryAgentLandingFinalization,
        transport: DeadlineMergeTrainGitHubTransport,
        store: OrdinaryAgentEffectStore,
        utc_seconds: Callable[[], float] = time.time,
    ) -> None:
        self._finalization = finalization
        self._transport = transport
        self._store = store
        self._utc_seconds = utc_seconds
        self._used = False

    def dispatch(self) -> str:
        finalized = self._finalization
        if self._used or finalized.disposition != "created":
            raise OrdinaryLandingDispatchStopped("landing_replay_no_dispatch")
        self._used = True
        command = finalized.effect.command
        preparation = finalized.preparation
        if command.kind != "pull_request_landing" or command.effect.merge_method != "merge":
            raise OrdinaryLandingDispatchStopped("landing_command_unsupported")
        effect = command.effect
        child = finalized.child
        if (
            child.effect_id != finalized.effect.effect_id
            or child.command_sha256 != finalized.effect.command_sha256
            or child.semantic_ordinal != finalized.effect.dispatch_count
            or child.custody_attempt_id != preparation.custody_attempt_id
            or child.controller_fence != preparation.controller_fence
            or child.admission_id != finalized.admission.admission_id
            or child.admission_binding_sha256 != finalized.admission.admission_binding_sha256
            or preparation.effect_id != finalized.effect.effect_id
            or preparation.state != "consumed"
        ):
            raise OrdinaryLandingDispatchStopped("landing_dispatch_admission_mismatch")
        if (
            effect.lineage.repository != preparation.target.repository
            or effect.lineage.base_branch != preparation.target.base_branch
            or effect.pull_request_number != preparation.entry.pull_request_number
            or effect.head_sha != preparation.entry.expected_head_sha
            or effect.rolling_base_sha != preparation.expected_base_sha
        ):
            raise OrdinaryLandingDispatchStopped("landing_command_scope_mismatch")
        try:
            self._transport.require_remaining(30)
            if (
                preparation.evidence is None
                or not 0
                <= self._utc_seconds() - preparation.evidence.observed_at
                <= LANDING_EVIDENCE_MAX_AGE_SECONDS
            ):
                raise OrdinaryAgentProviderDeferred()
        except OrdinaryAgentProviderDeferred:
            self._store.record_ordinary_semantic_outcome(
                child_id=finalized.child.child_id,
                typed_outcome=OrdinaryAgentKnownNotDispatchedOutcome(
                    reason="provider_attempt_deadline"
                ),
            )
            raise
        try:
            payload = self._transport.request(
                method="PUT",
                path=f"/repos/{effect.lineage.repository}/pulls/{effect.pull_request_number}/merge",
                body={"sha": effect.head_sha, "merge_method": "merge"},
                minimum_remaining_seconds=30,
            )
        except OrdinaryAgentProviderDeferred:
            self._store.record_ordinary_semantic_outcome(
                child_id=finalized.child.child_id,
                typed_outcome=OrdinaryAgentKnownNotDispatchedOutcome(
                    reason="provider_attempt_deadline"
                ),
            )
            raise
        except MergeTrainGitHubError as error:
            if error.status_code in {404, 405, 409}:
                self._store.record_ordinary_semantic_outcome(
                    child_id=finalized.child.child_id,
                    typed_outcome=OrdinaryAgentKnownNotDispatchedOutcome(
                        reason="provider_rejected"
                    ),
                )
            else:
                self._unknown()
            raise
        except Exception:
            self._unknown()
            raise
        try:
            if not isinstance(payload, dict) or payload.get("merged") is not True:
                raise OrdinaryAgentProviderEvidenceError("landing_merge_response_unproven")
            result_sha = payload.get("sha")
            if not isinstance(result_sha, str) or not result_sha:
                raise OrdinaryAgentProviderEvidenceError("landing_merge_response_unproven")
            owner, name = effect.lineage.repository.split("/", 1)
            data = require_complete_graphql_data(
                self._transport.request(
                    method="POST",
                    path="/graphql",
                    body={
                        "query": """query($owner: String!, $name: String!, $base: String!) {
                      rateLimit { cost }
                      repository(owner: $owner, name: $name) {
                        databaseId ref(qualifiedName: $base) {
                          target { ... on Commit { oid tree { oid }
                            parents(first: 3) { totalCount pageInfo { hasNextPage } nodes { oid } }
                          } }
                        }
                      }
                    }""",
                        "variables": {
                            "owner": owner,
                            "name": name,
                            "base": "refs/heads/" + effect.lineage.base_branch,
                        },
                    },
                ),
                transport=self._transport,
            )
            repository = _object(data.get("repository"))
            commit = _object(_object(repository.get("ref")).get("target"))
            parents = require_complete_connection(
                commit.get("parents"), label="landing_result_parents"
            )
            parent_shas = tuple(parent.get("oid") for parent in parents)
            if (
                repository.get("databaseId") != preparation.target.repository_id
                or commit.get("oid") != result_sha
                or _object(commit.get("tree")).get("oid") != preparation.expected_merge_tree_sha
                or parent_shas != (effect.rolling_base_sha, effect.head_sha)
            ):
                raise OrdinaryAgentProviderEvidenceError("landing_result_proof_mismatch")
            outcome = OrdinaryAgentCompletedOutcome(
                result_sha=result_sha,
                proof=OrdinaryAgentRefObservation(
                    repository=effect.lineage.repository,
                    ref="refs/heads/" + effect.lineage.base_branch,
                    sha=result_sha,
                    tree_sha=preparation.expected_merge_tree_sha,
                    parents=(effect.rolling_base_sha, effect.head_sha),
                ),
            )
        except Exception:
            self._unknown()
            raise
        # If persistence fails, leave the dispatch unresolved. Never manufacture
        # a competing outcome or repeat the PUT to recover a lost DB response.
        self._store.record_ordinary_semantic_outcome(
            child_id=finalized.child.child_id, typed_outcome=outcome
        )
        return result_sha

    def _unknown(self) -> None:
        self._store.record_ordinary_semantic_outcome(
            child_id=self._finalization.child.child_id,
            typed_outcome=OrdinaryAgentUnknownOutcome(reason="response_ambiguous"),
        )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError("landing_result_proof_missing")
    return value
