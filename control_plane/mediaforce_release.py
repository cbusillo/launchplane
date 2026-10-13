"""Source-side macOS release adapter; no CLI or installed-runtime activation.

The host boundary owns producer admission fencing, process custody and immutable
runtime replacement. The durable provider runner owns cross-worker reservations,
leases, original request custody and uncertain-effect reconciliation.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.mediaforce_release import (
    MediaforceBackup,
    MediaforceCompletionReceipt,
    MediaforceHostReceipt,
    MediaforceQuiescence,
    MediaforceReleasePlan,
    MediaforceReleaseRequest,
    MediaforceReleaseResult,
    MediaforceRuntime,
    MediaforceRuntimeCheck,
)
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.release_review import checklist_digest
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.service_auth import authz_policy_allows_immutable_github_id_administration
from control_plane.storage.postgres import PostgresRecordStore

MEDIAFORCE_RELEASE_REQUEST_ROUTE = "/v1/mediaforce-release-requests"
MEDIAFORCE_RELEASE_EXECUTION_ROUTE = "/internal/mediaforce-releases"


class MediaforceReleaseNotReady(ValueError):
    """A known gate refusal, before replacement; never a provider uncertainty."""


class MediaforceReleaseHost(Protocol):
    """System transport, not an agent-supplied command or deployment script.

    Every mutation must enforce the target fence token remotely. Fence acquisition
    must atomically reject another operation and stop admissions; it must not kill
    active work. Read-only observation must use a durable host completion receipt,
    including fence disposition. Runtime identity alone cannot prove completion.
    """

    def fence(self, plan: MediaforceReleasePlan, operation_key: str) -> MediaforceHostReceipt: ...

    def quiescence(self, fence: MediaforceHostReceipt) -> MediaforceQuiescence: ...

    def backup(
        self, plan: MediaforceReleasePlan, fence: MediaforceHostReceipt
    ) -> MediaforceBackup: ...

    def replace(self, runtime: MediaforceRuntime, fence: MediaforceHostReceipt) -> None:
        """A normal return proves the replacement completed; exceptions are unknown."""
        ...

    def check(self, fence: MediaforceHostReceipt) -> MediaforceRuntimeCheck: ...

    def finish(
        self, result: MediaforceReleaseResult, fence: MediaforceHostReceipt
    ) -> MediaforceCompletionReceipt:
        """Durably bind the result and resume admissions; replay uses this receipt."""
        ...

    def observe(
        self, plan: MediaforceReleasePlan, operation_key: str
    ) -> MediaforceCompletionReceipt | None:
        """Return an exact terminal receipt with released fence, or unknown (None)."""
        ...


@dataclass(frozen=True)
class MediaforceReleaseAuthority:
    store: PostgresRecordStore
    # Activation supplies a record-backed resolver. It verifies main ancestry,
    # artifact bytes/provenance, passing prior deployment and host placement.
    resolve_plan: Callable[[MediaforceReleaseRequest], MediaforceReleasePlan]

    def verify_plan(self, plan: MediaforceReleasePlan) -> None:
        if self.resolve_plan(plan.request) != plan:
            raise MediaforceReleaseNotReady("The recorded release inputs changed.")

    def _standing_director(self, decision: ReleaseReviewDecisionRecord) -> bool:
        try:
            policy = read_active_authz_policy_record(self.store).policy
        except (LookupError, TypeError):
            return False
        return authz_policy_allows_immutable_github_id_administration(
            policy=policy, github_id=int(decision.actor_github_id)
        )

    def acceptance(self, plan: MediaforceReleasePlan) -> ReleaseReviewDecisionRecord:
        self.verify_plan(plan)
        profile = self.store.read_product_profile_record(plan.request.product)
        latest = self.store.list_release_review_decision_records(product=profile.product, limit=1)
        if not latest:
            raise MediaforceReleaseNotReady("No recorded release acceptance.")
        decision = latest[0]
        if (
            not profile.is_active
            or profile.driver_id != "mediaforce-macos"
            or profile.repository.casefold() != plan.repository.casefold()
            or not any(
                lane.context == plan.context and lane.instance == "prod" for lane in profile.lanes
            )
            or profile.release_on_acceptance == "held"
            or decision.decision != "accepted"
            or decision.release_start != "promote"
            or not decision.release_issue_url
            or not profile.owner.github_id
            or decision.actor_github_id != profile.owner.github_id
            or decision.checklist.owner_github_id != profile.owner.github_id
            or decision.checklist.repository.casefold() != plan.repository.casefold()
            or decision.checklist.candidate != plan.candidate.version
            or decision.checklist.production != plan.previous.version
            or decision.checklist_digest != checklist_digest(decision.checklist)
            or (
                decision.acceptance_source == "director_standing"
                and (
                    profile.release_on_acceptance != "director_standing"
                    or not self._standing_director(decision)
                )
            )
        ):
            raise MediaforceReleaseNotReady("The current acceptance does not start this release.")
        return decision


@dataclass(frozen=True)
class _RequestAdapter:
    plan: MediaforceReleasePlan

    def target_key(self) -> str:
        return f"mediaforce-request:{canonical_json_sha256(self.plan.model_dump(mode='json'))}"

    def reconciliation_key(self) -> str:
        return self.target_key()

    def observe(self, _key: str, _phase: str, _reconciliation: str) -> ProviderObservation:
        return ProviderObservation(outcome="unknown")

    def apply(self, _key: str, _lease: ProviderOperationLease) -> ProviderMutationOutcome:
        return ProviderMutationOutcome(
            202,
            {"status": "requested", "plan": self.plan.model_dump(mode="json")},
            provider_effect_performed=False,
        )


def request_mediaforce_release(
    *,
    authority: MediaforceReleaseAuthority,
    request: MediaforceReleaseRequest,
    idempotency_key: str,
    requester_scope: str,
    trace_id: str,
) -> DurableProviderOperationResult:
    """Record intent only; neither acceptance nor a host mutation is supplied."""
    existing = authority.store.read_idempotency_record(
        scope=requester_scope,
        route_path=MEDIAFORCE_RELEASE_REQUEST_ROUTE,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        intent = authority.store.read_original_mutation_intent(existing)
        plan = MediaforceReleasePlan.model_validate(intent.get("plan"))
    else:
        plan = authority.resolve_plan(request)
        if plan.request != request:
            raise ValueError("Resolved release inputs must match the requested product and commit.")
        intent = {"product": request.product, "plan": plan.model_dump(mode="json")}
    return run_durable_provider_operation(
        store=authority.store,
        scope=requester_scope,
        route_path=MEDIAFORCE_RELEASE_REQUEST_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=canonical_json_sha256(request.model_dump(mode="json")),
        lease_owner=trace_id,
        response_trace_id=trace_id,
        adapter=_RequestAdapter(plan),
        original_intent=intent,
    )


@dataclass(frozen=True)
class MediaforceReleaseAdapter:
    plan: MediaforceReleasePlan
    authority: MediaforceReleaseAuthority
    host: MediaforceReleaseHost
    decision_record_id: str
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def target_key(self) -> str:
        # Same physical target shares one fence across products and requests.
        return f"mediaforce-runtime:{self.plan.target_id}"

    def reconciliation_key(self) -> str:
        return f"{self.target_key()}:{canonical_json_sha256(self.plan.model_dump(mode='json'))}"

    def _receipt(
        self,
        receipt: MediaforceHostReceipt,
        fence: MediaforceHostReceipt,
        *,
        fresh: bool = True,
    ) -> None:
        if (
            receipt.target_id != self.plan.target_id
            or receipt.operation_key != fence.operation_key
            or receipt.fence_token != fence.fence_token
            or receipt.observed_at < fence.observed_at
            or (
                fresh
                and not -timedelta(seconds=5)
                <= self.now() - receipt.observed_at
                <= timedelta(seconds=30)
            )
        ):
            raise ProviderMutationUnknownError(
                "Host evidence is stale or belongs to another fence."
            )

    def _forward(
        self, decision: ReleaseReviewDecisionRecord, lease: ProviderOperationLease
    ) -> None:
        lease.assert_current()
        if self.authority.acceptance(self.plan) != decision:
            raise MediaforceReleaseNotReady("Release acceptance changed.")

    def _idle(self, fence: MediaforceHostReceipt) -> MediaforceQuiescence:
        evidence = self.host.quiescence(fence)
        self._receipt(evidence, fence)
        return evidence

    def _check(
        self, fence: MediaforceHostReceipt, runtime: MediaforceRuntime
    ) -> MediaforceRuntimeCheck:
        evidence = self.host.check(fence)
        self._receipt(evidence, fence)
        if evidence.runtime != runtime:
            raise ProviderMutationUnknownError("The host runtime identity is uncertain.")
        return evidence

    def _complete(
        self,
        result: MediaforceReleaseResult,
        fence: MediaforceHostReceipt,
        lease: ProviderOperationLease,
    ) -> ProviderMutationOutcome:
        lease.checkpoint_effect("resume_admissions")
        completion = self.host.finish(result, fence)
        self._receipt(completion, fence)
        if completion.result != result:
            raise ProviderMutationUnknownError("Host completion does not match this release.")
        return ProviderMutationOutcome(200, result.model_dump(mode="json"))

    def apply(self, operation_key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        try:
            decision = self.authority.acceptance(self.plan)
            if decision.record_id != self.decision_record_id:
                raise MediaforceReleaseNotReady("The admitted acceptance was replaced.")
        except (MediaforceReleaseNotReady, FileNotFoundError) as error:
            raise ProviderMutationRejectedError(error) from error
        lease.checkpoint_effect("fence_admissions")
        fence = self.host.fence(self.plan, operation_key)
        if fence.operation_key != operation_key:
            raise ProviderMutationUnknownError("Fence belongs to another release.")
        self._receipt(fence, fence)
        if not self._idle(fence).idle:
            return self._complete(
                MediaforceReleaseResult(
                    status="blocked",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    reason="work_not_idle",
                ),
                fence,
                lease,
            )
        try:
            self._forward(decision, lease)
        except (MediaforceReleaseNotReady, FileNotFoundError):
            return self._complete(
                MediaforceReleaseResult(
                    status="blocked",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    reason="acceptance_changed",
                ),
                fence,
                lease,
            )
        lease.checkpoint_effect("database_backup")
        backup = self.host.backup(self.plan, fence)
        self._receipt(backup, fence)
        if backup.database_id != self.plan.database_id or backup.runtime != self.plan.previous:
            raise ProviderMutationUnknownError("Backup belongs to a different database or runtime.")
        if not backup.verified:
            return self._complete(
                MediaforceReleaseResult(
                    status="blocked",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    backup=backup,
                    reason="backup_unverified",
                ),
                fence,
                lease,
            )
        try:
            self._forward(decision, lease)
        except (MediaforceReleaseNotReady, FileNotFoundError):
            return self._complete(
                MediaforceReleaseResult(
                    status="blocked",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    backup=backup,
                    reason="acceptance_changed",
                ),
                fence,
                lease,
            )
        idle = self._idle(fence)
        if not idle.idle:
            return self._complete(
                MediaforceReleaseResult(
                    status="blocked",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    backup=backup,
                    reason="work_not_idle",
                ),
                fence,
                lease,
            )
        try:
            self._forward(decision, lease)
        except (MediaforceReleaseNotReady, FileNotFoundError):
            return self._complete(
                MediaforceReleaseResult(
                    status="blocked",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    backup=backup,
                    reason="acceptance_changed",
                ),
                fence,
                lease,
            )
        lease.checkpoint_effect("replace_runtime")
        self.host.replace(self.plan.candidate, fence)
        check = self._check(fence, self.plan.candidate)
        if check.healthy and check.post_checks_passed:
            return self._complete(
                MediaforceReleaseResult(
                    status="passed",
                    plan=self.plan,
                    decision_record_id=decision.record_id,
                    backup=backup,
                    runtime_check=check,
                    quiescence=idle,
                ),
                fence,
                lease,
            )
        # Compensation belongs to the admitted effect even if forward acceptance
        # was revoked. It replaces only the pinned runtime, never database data.
        lease.checkpoint_effect("recover_prior_runtime")
        self.host.replace(self.plan.previous, fence)
        recovery = self._check(fence, self.plan.previous)
        if not recovery.healthy or not recovery.post_checks_passed:
            raise ProviderMutationUnknownError(
                "The prior runtime did not qualify; admissions held."
            )
        return self._complete(
            MediaforceReleaseResult(
                status="failed_recovered",
                plan=self.plan,
                decision_record_id=decision.record_id,
                backup=backup,
                runtime_check=check,
                recovery_check=recovery,
                quiescence=idle,
                reason="post_checks_failed",
            ),
            fence,
            lease,
        )

    def observe(
        self, operation_key: str, _phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation(outcome="unknown")
        receipt = self.host.observe(self.plan, operation_key)
        if (
            receipt is None
            or receipt.result.plan != self.plan
            or receipt.result.decision_record_id != self.decision_record_id
            or receipt.operation_key != operation_key
            or receipt.target_id != self.plan.target_id
        ):
            return ProviderObservation(outcome="unknown")
        result = receipt.result
        # Exact host receipts are required; a currently healthy process alone
        # cannot settle a lost replacement, backup, recovery or fence release.
        check = (
            result.recovery_check if result.status == "failed_recovered" else result.runtime_check
        )
        if result.status != "blocked":
            expected = (
                self.plan.previous if result.status == "failed_recovered" else self.plan.candidate
            )
            if (
                check is None
                or check.runtime != expected
                or not check.healthy
                or not check.post_checks_passed
                or check.operation_key != operation_key
                or check.target_id != self.plan.target_id
                or result.backup is None
                or not result.backup.verified
                or result.backup.database_id != self.plan.database_id
                or result.backup.runtime != self.plan.previous
            ):
                return ProviderObservation(outcome="unknown")
        return ProviderObservation(
            outcome="present",
            response_status_code=200,
            response_payload=result.model_dump(mode="json"),
        )


def run_mediaforce_release(
    *,
    authority: MediaforceReleaseAuthority,
    host: MediaforceReleaseHost,
    requester_scope: str,
    request_key: str,
    worker_id: str,
    trace_id: str,
) -> DurableProviderOperationResult:
    """System worker entrypoint: load an existing request, never caller deploy inputs."""
    request = authority.store.read_idempotency_record(
        scope=requester_scope,
        route_path=MEDIAFORCE_RELEASE_REQUEST_ROUTE,
        idempotency_key=request_key,
    )
    if request is None or request.state != "completed":
        raise MediaforceReleaseNotReady("No completed release request is recorded.")
    intent = authority.store.read_original_mutation_intent(request)
    plan = MediaforceReleasePlan.model_validate(intent.get("plan"))
    if (
        intent.get("product") != plan.request.product
        or canonical_json_sha256(plan.request.model_dump(mode="json"))
        != request.request_fingerprint
    ):
        raise MediaforceReleaseNotReady("The release request provenance is invalid.")
    execution = authority.store.read_idempotency_record(
        scope="system:mediaforce-release",
        route_path=MEDIAFORCE_RELEASE_EXECUTION_ROUTE,
        idempotency_key=request.record_id,
    )
    if execution is None:
        decision_id = authority.acceptance(plan).record_id
        execution_intent = {**intent, "decision_record_id": decision_id}
    else:
        execution_intent = authority.store.read_original_mutation_intent(execution)
        recorded_decision_id = execution_intent.get("decision_record_id")
        if (
            not isinstance(recorded_decision_id, str)
            or not recorded_decision_id
            or execution_intent.get("plan") != intent.get("plan")
        ):
            raise MediaforceReleaseNotReady("The admitted release provenance is unavailable.")
        decision_id = recorded_decision_id
    return run_durable_provider_operation(
        store=authority.store,
        scope="system:mediaforce-release",
        route_path=MEDIAFORCE_RELEASE_EXECUTION_ROUTE,
        idempotency_key=request.record_id,
        request_fingerprint=canonical_json_sha256(execution_intent),
        lease_owner=worker_id,
        response_trace_id=trace_id,
        adapter=MediaforceReleaseAdapter(plan, authority, host, decision_id),
        original_intent=execution_intent,
    )
