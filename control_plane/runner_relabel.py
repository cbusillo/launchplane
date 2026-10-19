"""Actor-owned native runner relabeling; no workflow or credential fallback."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runner_lane_registration import (
    RunnerLaneRegistrationAuditRecord,
    RunnerLaneRegistrationPolicy,
    RunnerLaneRegistrationRequest,
)
from control_plane.contracts.runner_relabel import (
    RunnerRelabelPolicy,
    RunnerRelabelReviewRequest,
    RunnerRelabelActor,
    RunnerRelabelPolicyRecord,
    RunnerRelabelReview,
)
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    mint_runner_relabel_installation_token,
    revoke_installation_token,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.runner_lane_github import GitHubRunnerLaneInventoryReader
from control_plane.secrets import resolve_context_secret_value
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubActionsIdentity,
    LocalOperatorIdentity,
    LocalAdminIdentity,
    LaunchplaneIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.runner_lane_relabel import _labels, execute_relabel

ROUTE = "/v1/runner-relabel/{policy_id}/operations"
RUNNER_SECRET_INTEGRATION = "runner-relabel"
RUNNER_SECRET_BINDING = "private_key"


class RunnerRelabelInactive(ValueError):
    """A named service activation prerequisite is absent."""


def actor_identity(identity: LaunchplaneIdentity) -> dict[str, object]:
    if isinstance(identity, GitHubHumanIdentity):
        return RunnerRelabelActor(type="github_human", github_id=identity.github_id).model_dump(
            mode="json"
        )
    kind = (
        "github_actions"
        if isinstance(identity, GitHubActionsIdentity)
        else "local_operator"
        if isinstance(identity, LocalOperatorIdentity)
        else "local_admin"
        if isinstance(identity, LocalAdminIdentity)
        else "terminal_agent"
    )
    return RunnerRelabelActor.model_validate(
        {"type": kind, "subject": identity.subject}
    ).model_dump(mode="json")


def actor_scope(actor: dict[str, object]) -> str:
    return "runner-relabel:" + canonical_json_sha256(actor)


def load_policy(store: PostgresRecordStore, policy_id: str) -> dict[str, Any]:
    saved = store.read_runner_relabel_policy(policy_id)
    if saved is None:
        raise RunnerRelabelInactive("runner_policy_missing")
    saved = RunnerRelabelPolicyRecord.model_validate(saved).model_dump(mode="json")
    digest = saved.get("policy_sha256")
    if digest != canonical_json_sha256({k: v for k, v in saved.items() if k != "policy_sha256"}):
        raise ValueError("Runner policy custody is invalid.")
    policy = RunnerRelabelPolicy.model_validate(saved["policy"])
    if policy.policy_id != policy_id:
        raise ValueError("Runner policy identity differs.")
    if not policy.enabled:
        raise RunnerRelabelInactive("runner_policy_disabled")
    return saved


@contextmanager
def runner_transport(
    store: PostgresRecordStore, policy: RunnerRelabelPolicy, *, write: bool
) -> Iterator[MergeTrainGitHubTransport]:
    capability = policy.capability
    if capability is None or not capability.accepted:
        raise RunnerRelabelInactive("runner_purpose_capability_not_accepted")
    private_key = resolve_context_secret_value(
        integration=RUNNER_SECRET_INTEGRATION,
        context_name=capability.secret_context,
        binding_key=RUNNER_SECRET_BINDING,
        database_url=store.database_url,
    )
    if not private_key:
        raise RunnerRelabelInactive("runner_purpose_managed_key_missing")
    token = mint_runner_relabel_installation_token(
        identity=GitHubAppIdentity(app_id=capability.app_id, private_key=private_key),
        installation_id=capability.installation_id,
        repository=policy.repository,
        repository_id=str(policy.repository_id),
        write=write,
    )
    try:
        yield UrllibMergeTrainGitHubTransport(token=token.token)
    finally:
        revoke_installation_token(installation_token=token)


def _request(
    policy: RunnerRelabelPolicy, review: dict[str, Any], *, mutate: bool
) -> RunnerLaneRegistrationRequest:
    return RunnerLaneRegistrationRequest(
        operation="relabel",
        repository=policy.repository,
        host_name=policy.host_name,
        lane_name=policy.runner_name,
        registration_root=policy.registration_root,
        expected_runner_id=policy.runner_id,
        labels=tuple(review["desired_custom_labels"]),
        expected_custom_labels=tuple(review["before_custom_labels"]),
        mutate=mutate,
        audit_record_key=review["review_id"],
    )


def _engine_policy(policy: RunnerRelabelPolicy) -> RunnerLaneRegistrationPolicy:
    return RunnerLaneRegistrationPolicy(
        allowed_repositories=(policy.repository,),
        approved_hosts=(policy.host_name,),
        allowed_registration_roots=(policy.registration_root,),
    )


def create_review(
    store: PostgresRecordStore,
    policy_id: str,
    body: RunnerRelabelReviewRequest,
    actor: dict[str, object],
) -> dict[str, Any]:
    saved = load_policy(store, policy_id)
    policy = RunnerRelabelPolicy.model_validate(saved["policy"])
    if body.inverse_of_review_id:
        original = store.read_runner_relabel_document("review", body.inverse_of_review_id)
        if original is None or original["actor"] != actor or original["policy_id"] != policy_id:
            raise ValueError("Original inverse review is unavailable to this actor.")
        if list(body.desired_custom_labels) != original["before_custom_labels"]:
            raise ValueError("Inverse labels must match the original before snapshot.")
        claim = store.read_runner_relabel_document("review-operation", body.inverse_of_review_id)
        if claim is None:
            raise ValueError("Inverse requires an original operation.")
    review: dict[str, Any] = {
        "review_id": uuid4().hex,
        "policy_id": policy_id,
        "policy_sha256": saved["policy_sha256"],
        "policy": policy.model_dump(mode="json"),
        "actor": actor,
        "desired_custom_labels": list(body.desired_custom_labels),
        "reason": body.reason,
        "inverse_of_review_id": body.inverse_of_review_id,
    }
    with runner_transport(store, policy, write=False) as transport:
        path = f"/repos/{policy.repository}/actions/runners/{policy.runner_id}/labels"
        before = _labels(transport.request(method="GET", path=path))
        review["before_custom_labels"] = list(before.custom)
        review["automatic_labels"] = list(before.automatic)
        execute_relabel(
            request=_request(policy, review, mutate=False),
            policy=_engine_policy(policy),
            transport=transport,
            inventory_reader=lambda: GitHubRunnerLaneInventoryReader(
                transport=transport
            ).read_runner_lane_inventory(repository=policy.repository),
            audit_poster=lambda audit, key: {},
            protected_lane_names=policy.protected_runner_names,
            protected_labels=policy.protected_labels,
        )
    if load_policy(store, policy_id)["policy_sha256"] != saved["policy_sha256"]:
        raise ValueError("Runner policy changed during review.")
    review["review_sha256"] = canonical_json_sha256(review)
    review = RunnerRelabelReview.model_validate(review).model_dump(mode="json")
    store.write_runner_relabel_document("review", review["review_id"], review)
    return review


@dataclass
class RunnerRelabelAdapter:
    store: PostgresRecordStore
    review: dict[str, Any]
    trace_id: str
    operation_key: str
    evidence_only: bool = False
    authorize_effect: Callable[[], None] = lambda: None

    @property
    def policy(self) -> RunnerRelabelPolicy:
        return RunnerRelabelPolicy.model_validate(self.review["policy"])

    def target_key(self) -> str:
        return f"github-runner:{self.policy.repository_id}:{self.policy.runner_id}"

    def reconciliation_key(self) -> str:
        return str(self.review["review_sha256"])

    def response(self, result: dict[str, object]) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.trace_id,
            records={"runner_relabel_review_id": self.review["review_id"]},
            result={
                "actor": self.review["actor"],
                "review_sha256": self.review["review_sha256"],
                **result,
            },
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        terminal = self.store.read_runner_relabel_document("terminal", provider_operation_key)
        if self.evidence_only:
            if (
                terminal is not None
                and terminal.get("status") == "completed"
                and terminal.get("review_sha256") == self.review["review_sha256"]
            ):
                return ProviderObservation(
                    "present",
                    response_payload=self.response(
                        {"status": "completed", "evidence_recovered": True}
                    ),
                )
            return ProviderObservation("unknown")
        try:
            current = load_policy(self.store, self.policy.policy_id)
            if current["policy_sha256"] != self.review["policy_sha256"]:
                return ProviderObservation("unknown")
            with runner_transport(self.store, self.policy, write=False) as transport:
                inventory = GitHubRunnerLaneInventoryReader(
                    transport=transport
                ).read_runner_lane_inventory(repository=self.policy.repository)
                matches = [
                    lane for lane in inventory.lanes if lane.github_id == self.policy.runner_id
                ]
                labels = _labels(
                    transport.request(
                        method="GET",
                        path=f"/repos/{self.policy.repository}/actions/runners/{self.policy.runner_id}/labels",
                    )
                )
                if (
                    len(matches) == 1
                    and matches[0].name.lower() == self.policy.runner_name
                    and labels.custom == tuple(self.review["desired_custom_labels"])
                    and labels.automatic == tuple(self.review["automatic_labels"])
                ):
                    return ProviderObservation(
                        "present",
                        response_payload=self.response(
                            {
                                "status": "observed_desired",
                                "provider_effect_attribution": "unknown",
                                "observed_custom_labels": list(labels.custom),
                            }
                        ),
                    )
        except Exception:
            pass  # Provider exceptions may contain credentials; unknown is not retry permission.
        return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        if self.evidence_only:
            raise ProviderMutationRejectedError(ValueError("Evidence recovery cannot mutate."))
        if not isinstance(lease, ProviderEvidenceLease):
            raise ProviderMutationRejectedError(
                ValueError("Native evidence requires a fenced lease.")
            )
        policy = self.policy
        evidence_lease = lease
        store = self.store
        review = self.review
        dispatched = False

        def post(audit: RunnerLaneRegistrationAuditRecord, key: str) -> dict[str, object]:
            store.write_runner_relabel_document(
                "planned" if audit.status == "planned" else "terminal-audit",
                provider_operation_key,
                audit.model_dump(mode="json"),
                reservation=evidence_lease.evidence_reservation(),
            )
            return {
                "status": "accepted",
                "records": {"runner_lane_registration_audit_record_key": audit.audit_record_key},
            }

        try:
            with runner_transport(store, policy, write=True) as transport:

                class FencedTransport:
                    def request(
                        self, *, method: str, path: str, body: dict[str, object] | None = None
                    ) -> object:
                        nonlocal dispatched
                        if method != "PUT":
                            result = transport.request(method=method, path=path, body=body)
                            if path.endswith("/labels") and _labels(result).automatic != tuple(
                                review["automatic_labels"]
                            ):
                                raise ValueError("Reviewed automatic runner labels changed.")
                            return result
                        expected_path = (
                            f"/repos/{policy.repository}/actions/runners/{policy.runner_id}/labels"
                        )
                        if path != expected_path or body != {
                            "labels": review["desired_custom_labels"]
                        }:
                            raise ValueError("Unexpected runner effect.")
                        self_authorize()
                        lease.checkpoint_effect("runner_custom_labels_put")
                        captured = evidence_lease.evidence_reservation()
                        with store.runner_relabel_policy_guard(policy.policy_id) as session:
                            current = store.read_runner_relabel_policy(
                                policy.policy_id, session=session
                            )
                            if (
                                current is None
                                or current["policy_sha256"] != review["policy_sha256"]
                            ):
                                raise ValueError("Reviewed runner policy changed.")
                            store.require_current_provider_execution(session, captured)
                            dispatched = True
                            return transport.request(method=method, path=path, body=body)

                self_authorize = self.authorize_effect
                guarded = FencedTransport()
                result = execute_relabel(
                    request=_request(policy, review, mutate=True),
                    policy=_engine_policy(policy),
                    transport=guarded,
                    inventory_reader=lambda: GitHubRunnerLaneInventoryReader(
                        transport=guarded
                    ).read_runner_lane_inventory(repository=policy.repository),
                    audit_poster=post,
                    protected_lane_names=policy.protected_runner_names,
                    protected_labels=policy.protected_labels,
                )
                if result.get("status") == "completed":
                    store.write_runner_relabel_document(
                        "terminal",
                        provider_operation_key,
                        {
                            "status": "completed",
                            "review_sha256": review["review_sha256"],
                            "result": result,
                        },
                        reservation=evidence_lease.evidence_reservation(),
                    )
                    return ProviderMutationOutcome(202, self.response(result))
                if dispatched or result.get("write_attempted"):
                    raise ProviderMutationUnknownError(
                        "Runner outcome requires original-key observation."
                    )
                raise ProviderMutationRejectedError(ValueError("Runner changed before dispatch."))
        except ProviderMutationUnknownError:
            raise
        except ProviderMutationRejectedError:
            raise
        except RunnerRelabelInactive as error:
            if dispatched:
                raise ProviderMutationUnknownError(
                    "Runner capability changed after dispatch."
                ) from error
            raise ProviderMutationRejectedError(error) from error
        except Exception as error:
            if dispatched:
                raise ProviderMutationUnknownError(
                    "Runner outcome or evidence is uncertain."
                ) from error
            raise ProviderMutationRejectedError(
                ValueError("Runner prerequisite is unavailable.")
            ) from error
