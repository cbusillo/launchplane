"""Custody-scoped semantic executor for one reserved ordinary-agent effect."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import time
from uuid import uuid4
from urllib.parse import quote

from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateHeadMergeOutcome,
    CandidateRefDeleteEffect,
    CandidateRefPrepareEffect,
    PullRequestHeadRefreshEffect,
    PullRequestLandingEffect,
    StackChildCloseEffect,
    StackChildCommentEffect,
    StackChildLabelEffect,
    StackChildMergeEffect,
)
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentCompletedOutcome,
    OrdinaryAgentAcceptedAsyncOutcome,
    OrdinaryAgentControllerFence,
    OrdinaryAgentEffectRecord,
    OrdinaryAgentEffectStore,
    OrdinaryAgentKnownNotDispatchedOutcome,
    OrdinaryAgentLabelObservation,
    OrdinaryAgentReconciliationObservation,
    OrdinaryAgentRefObservation,
    OrdinaryAgentUnknownOutcome,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    LegacyMergeTrainEffectExecutor,
    MergeTrainGitHubError,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    ordinary_agent_provider_token_lease,
)
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    ORDINARY_MUTATION_WORK_SECONDS,
    require_installation_provider_ready,
)
from control_plane.ordinary_agent_effect_lifecycle import ordinary_agent_comment_body
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.workflows.launchplane import github_api_request


TransportFactory = Callable[[str], MergeTrainGitHubTransport]
DispatchOutcome = OrdinaryAgentCompletedOutcome | OrdinaryAgentAcceptedAsyncOutcome


class OrdinaryAgentEffectTerminal(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class OrdinaryAgentMergeTrainEffectExecutor:
    """Execute exactly the command in one already-reserved effect record."""

    def __init__(
        self,
        *,
        record: OrdinaryAgentEffectRecord,
        controller_fence: OrdinaryAgentControllerFence,
        effect_store: OrdinaryAgentEffectStore,
        custody_store: OrdinaryAgentCustodyAttemptStore,
        secret_store: OrdinaryAgentCustodySecretStore,
        api_request: GitHubApiRequest = github_api_request,
        transport_factory: TransportFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._record = record
        self._controller_fence = controller_fence
        self._effect_store = effect_store
        self._custody_store = custody_store
        self._secret_store = secret_store
        self._api_request = api_request
        self._transport_factory = transport_factory or (
            lambda token: UrllibMergeTrainGitHubTransport(token=token)
        )
        self._monotonic = monotonic
        self._utc_now = utc_now

    def prepare_candidate_ref(self, effect: CandidateRefPrepareEffect) -> None:
        self._require_command("candidate_ref_prepare", effect)

        def call(client: GitHubMergeTrainClient) -> OrdinaryAgentCompletedOutcome:
            try:
                response = client.transport.request(
                    method="POST",
                    path=f"/repos/{effect.lineage.repository}/git/refs",
                    body={"ref": effect.candidate_ref, "sha": effect.base_sha},
                )
            except MergeTrainGitHubError as error:
                if error.status_code not in {409, 422}:
                    raise
                response = client.transport.request(
                    method="PATCH",
                    path=(
                        f"/repos/{effect.lineage.repository}/git/refs/"
                        f"{quote(effect.candidate_ref.removeprefix('refs/'), safe='/')}"
                    ),
                    body={"sha": effect.base_sha, "force": True},
                )
            if not isinstance(response, Mapping) or response.get("ref") != effect.candidate_ref:
                raise MergeTrainGitHubError("candidate_ref_prepare_evidence_mismatch")
            target = response.get("object")
            if not isinstance(target, Mapping) or target.get("sha") != effect.base_sha:
                raise MergeTrainGitHubError("candidate_ref_prepare_evidence_mismatch")
            proof = OrdinaryAgentRefObservation(
                repository=effect.lineage.repository,
                ref=effect.candidate_ref,
                sha=effect.base_sha,
            )
            return OrdinaryAgentCompletedOutcome(proof=proof)

        self._dispatch(call)

    def merge_candidate_head(self, effect: CandidateHeadMergeEffect) -> CandidateHeadMergeOutcome:
        self._require_command("candidate_head_merge", effect)
        returned: CandidateHeadMergeOutcome | None = None

        def call(client: GitHubMergeTrainClient) -> OrdinaryAgentCompletedOutcome:
            nonlocal returned
            returned = LegacyMergeTrainEffectExecutor(client=client).merge_candidate_head(effect)
            if returned.result_sha is None:
                proof = _read_ref_proof(
                    client.transport, effect.lineage.repository, effect.candidate_ref
                ).model_copy(update={"contained_head_sha": effect.head_sha})
                if proof.sha != effect.rolling_parent_sha:
                    raise MergeTrainGitHubError("candidate_merge_no_op_unproven")
                comparison = client.transport.request(
                    method="GET",
                    path=f"/repos/{effect.lineage.repository}/compare/{effect.head_sha}...{effect.rolling_parent_sha}",
                )
                if (
                    not isinstance(comparison, Mapping)
                    or comparison.get("status") not in {"ahead", "identical"}
                    or not isinstance(comparison.get("base_commit"), Mapping)
                    or comparison["base_commit"].get("sha") != effect.head_sha
                    or not isinstance(comparison.get("merge_base_commit"), Mapping)
                    or comparison["merge_base_commit"].get("sha") != effect.head_sha
                ):
                    raise MergeTrainGitHubError("candidate_merge_no_op_unproven")
                returned = CandidateHeadMergeOutcome(
                    result_sha=None,
                    result_tree_sha=proof.tree_sha,
                )
                return OrdinaryAgentCompletedOutcome(result_sha=proof.sha, no_op=True, proof=proof)
            if not returned.result_tree_sha or returned.parent_shas != (
                effect.rolling_parent_sha,
                effect.head_sha,
            ):
                raise MergeTrainGitHubError("candidate_merge_response_proof_incomplete")
            proof = OrdinaryAgentRefObservation(
                repository=effect.lineage.repository,
                ref=effect.candidate_ref,
                sha=returned.result_sha,
                tree_sha=returned.result_tree_sha,
                parents=returned.parent_shas,
            )
            return OrdinaryAgentCompletedOutcome(result_sha=proof.sha, proof=proof)

        self._dispatch(call)
        if returned is None:
            raise RuntimeError("candidate merge completed without a closed result")
        return returned

    def refresh_pull_request_head(self, effect: PullRequestHeadRefreshEffect) -> None:
        self._require_command("pull_request_head_refresh", effect)

        def call(client: GitHubMergeTrainClient) -> DispatchOutcome:
            LegacyMergeTrainEffectExecutor(client=client).refresh_pull_request_head(effect)
            return OrdinaryAgentAcceptedAsyncOutcome()

        self._dispatch(call)

    def merge_stack_child(self, effect: StackChildMergeEffect) -> str:
        self._require_command("stack_child_merge", effect)
        result = ""

        def call(client: GitHubMergeTrainClient) -> OrdinaryAgentCompletedOutcome:
            nonlocal result
            result = LegacyMergeTrainEffectExecutor(client=client).merge_stack_child(effect)
            proof = _read_commit_proof(
                client.transport,
                effect.lineage.repository,
                result,
                ref=effect.parent_head_ref,
            )
            return OrdinaryAgentCompletedOutcome(result_sha=result, proof=proof)

        self._dispatch(call)
        return result

    def land_pull_request(self, effect: PullRequestLandingEffect) -> str:
        self._require_command("pull_request_landing", effect)
        raise OrdinaryAgentEffectTerminal("landing_requires_joined_finalization")

    def comment_stack_child(self, effect: StackChildCommentEffect) -> str:
        self._require_command("stack_child_comment", effect)
        result = ""

        def call(client: GitHubMergeTrainClient) -> OrdinaryAgentCompletedOutcome:
            nonlocal result
            result = client.comment_pull_request(
                repository=effect.lineage.repository,
                pull_request_number=effect.pull_request_number,
                body=ordinary_agent_comment_body(
                    body=effect.body, effect_id=self._record.effect_id
                ),
            )
            return OrdinaryAgentCompletedOutcome(result_id=result)

        self._dispatch(call)
        return result

    def label_stack_child(self, effect: StackChildLabelEffect) -> None:
        self._require_command("stack_child_label", effect)

        def already_present(client: GitHubMergeTrainClient) -> OrdinaryAgentLabelObservation | None:
            payload = client.transport.request(
                method="GET",
                path=(
                    f"/repos/{effect.lineage.repository}/issues/"
                    f"{effect.pull_request_number}/labels?per_page=100"
                ),
            )
            if not isinstance(payload, list):
                raise MergeTrainGitHubError("ordinary_label_observation_malformed")
            if any(
                not isinstance(item, dict) or not isinstance(item.get("name"), str)
                for item in payload
            ):
                raise MergeTrainGitHubError("ordinary_label_observation_malformed")
            if any(item["name"] == effect.label for item in payload):
                return OrdinaryAgentLabelObservation(
                    repository=effect.lineage.repository,
                    number=effect.pull_request_number,
                    label=effect.label,
                    present=True,
                )
            if len(payload) >= 100:
                raise MergeTrainGitHubError("ordinary_label_observation_incomplete")
            return None

        def call(client: GitHubMergeTrainClient) -> OrdinaryAgentCompletedOutcome:
            LegacyMergeTrainEffectExecutor(client=client).label_stack_child(effect)
            return OrdinaryAgentCompletedOutcome()

        self._dispatch(call, no_dispatch_preflight=already_present)

    def close_stack_child(self, effect: StackChildCloseEffect) -> None:
        self._require_command("stack_child_close", effect)

        def call(client: GitHubMergeTrainClient) -> OrdinaryAgentCompletedOutcome:
            LegacyMergeTrainEffectExecutor(client=client).close_stack_child(effect)
            return OrdinaryAgentCompletedOutcome()

        self._dispatch(call)

    def delete_candidate_ref(self, effect: CandidateRefDeleteEffect) -> bool:
        self._require_command("candidate_ref_delete", effect)
        self._effect_store.complete_ordinary_effect_without_dispatch(
            effect_id=self._record.effect_id,
            expected_effect_revision=self._record.revision,
            disposition="candidate_ref_retained_no_conditional_delete",
        )
        return False

    def _dispatch(
        self,
        call: Callable[[GitHubMergeTrainClient], DispatchOutcome],
        *,
        no_dispatch_preflight: Callable[
            [GitHubMergeTrainClient], OrdinaryAgentLabelObservation | None
        ]
        | None = None,
    ) -> None:
        reservation = self._effect_store.reserve_ordinary_custody_attempt(
            effect_id=self._record.effect_id,
            expected_effect_revision=self._record.revision,
        )
        started = self._monotonic()
        child_id: str | None = None
        outcome_recorded = False
        try:
            with ordinary_agent_provider_token_lease(
                record_store=self._custody_store,
                secret_store=self._secret_store,
                candidate=reservation.candidate,
                idempotency_key=reservation.idempotency_key,
                request_payload=reservation.request_payload,
                api_request=self._api_request,
                monotonic=self._monotonic,
                utc_now=self._utc_now,
                before_token_mint=lambda app_id, installation_id: (
                    require_installation_provider_ready(
                        app_id=app_id,
                        installation_id=installation_id,
                        resource_classes=("core", "secondary"),
                        read_provider_wait=self._effect_store.read_provider_wait,
                        utc_now=self._utc_now,
                    )
                ),
            ) as lease:
                expiry = datetime.fromisoformat(
                    lease.installation_token.expires_at.replace("Z", "+00:00")
                )
                transport = DeadlineMergeTrainGitHubTransport(
                    transport=self._transport_factory(lease.installation_token.token),
                    work_deadline=started + ORDINARY_MUTATION_WORK_SECONDS,
                    token_deadline=self._monotonic()
                    + max(0, expiry.timestamp() - self._utc_now().timestamp()),
                    monotonic=self._monotonic,
                )
                client = GitHubMergeTrainClient(transport=transport)
                observed_label = no_dispatch_preflight(client) if no_dispatch_preflight else None
                if observed_label is not None:
                    self._effect_store.complete_ordinary_effect_without_dispatch(
                        effect_id=self._record.effect_id,
                        expected_effect_revision=reservation.effect_revision,
                        disposition="label_already_present",
                        typed_observation=OrdinaryAgentReconciliationObservation(
                            observation_id="label-" + uuid4().hex,
                            custody_attempt_id=reservation.attempt_id,
                            observed_at=int(self._utc_now().timestamp()),
                            observation=observed_label,
                        ),
                    )
                    outcome_recorded = True
                    return
                child = self._effect_store.checkpoint_ordinary_semantic_dispatch(
                    effect_id=self._record.effect_id,
                    controller_fence=self._controller_fence,
                    custody_attempt_id=reservation.attempt_id,
                    fixed_token_expires_at=int(expiry.timestamp()),
                )
                child_id = child.child_id
                try:
                    outcome = call(client)
                except MergeTrainGitHubError as error:
                    if error.status_code not in {400, 401, 403, 404, 409, 422}:
                        raise
                    reason = (
                        "non_mergeable"
                        if self._record.command.kind == "candidate_head_merge"
                        and error.status_code == 409
                        else "provider_rejected"
                    )
                    self._effect_store.record_ordinary_semantic_outcome(
                        child_id=child_id,
                        typed_outcome=OrdinaryAgentKnownNotDispatchedOutcome(
                            reason=reason  # type: ignore[arg-type]
                        ),
                    )
                    outcome_recorded = True
                    raise OrdinaryAgentEffectTerminal(reason) from error
                else:
                    self._effect_store.record_ordinary_semantic_outcome(
                        child_id=child_id,
                        typed_outcome=outcome,
                    )
                    outcome_recorded = True
        except Exception:
            if child_id is not None and not outcome_recorded:
                self._effect_store.record_ordinary_semantic_outcome(
                    child_id=child_id,
                    typed_outcome=OrdinaryAgentUnknownOutcome(reason="response_ambiguous"),
                )
            raise

    def _require_command(self, kind: str, effect: object) -> None:
        if self._record.command.kind != kind or self._record.command.effect != effect:
            raise PermissionError("ordinary_effect_command_mismatch")


def _read_ref_proof(
    transport: MergeTrainGitHubTransport, repository: str, reference: str
) -> OrdinaryAgentRefObservation:
    sha = _read_ref_sha(transport, repository, reference)
    commit = _read_commit_proof(transport, repository, sha, ref=reference)
    return commit


def _read_ref_sha(transport: MergeTrainGitHubTransport, repository: str, reference: str) -> str:
    path = reference.removeprefix("refs/")
    payload = transport.request(
        method="GET",
        path=f"/repos/{repository}/git/ref/{quote(path, safe='/')}",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("object"), dict):
        raise MergeTrainGitHubError("ordinary_ref_proof_malformed")
    sha = str(payload["object"].get("sha") or "").strip()
    if not sha:
        raise MergeTrainGitHubError("ordinary_ref_proof_malformed")
    return sha


def _read_commit_proof(
    transport: MergeTrainGitHubTransport,
    repository: str,
    sha: str,
    *,
    ref: str,
) -> OrdinaryAgentRefObservation:
    payload = transport.request(
        method="GET",
        path=f"/repos/{repository}/git/commits/{quote(sha, safe='')}",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), dict):
        raise MergeTrainGitHubError("ordinary_commit_proof_malformed")
    parents = payload.get("parents")
    if not isinstance(parents, list):
        raise MergeTrainGitHubError("ordinary_commit_proof_malformed")
    return OrdinaryAgentRefObservation(
        repository=repository,
        ref=ref,
        sha=str(payload.get("sha") or "").strip(),
        tree_sha=str(payload["tree"].get("sha") or "").strip(),
        parents=tuple(
            str(parent.get("sha") or "").strip() for parent in parents if isinstance(parent, dict)
        ),
    )
