from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Literal
from urllib.parse import quote, urlsplit

from control_plane.advisory_check_projection import write_advisory_check_projection
from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckProjection,
    AdvisoryCheckConclusion,
    AdvisoryCheckProjectionResult,
    AdvisoryCheckRunStatus,
    OWNER_ACCEPTANCE_CHECK_NAME,
)
from control_plane.contracts.change_impact import ChangeImpactTarget, ChangeImpactTargetReference
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.github_app_identity import (
    GitHubAppInstallationToken,
    revoke_installation_token,
)
from control_plane.owner_acceptance import (
    evaluate_owner_acceptance,
    require_owner_acceptance_projection_lock_store,
)
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]
GitHubAppTokenProvider = Callable[[str, str], GitHubAppInstallationToken]
OwnerAcceptanceEventPersistenceOutcome = Literal["absent", "unknown"]
OWNER_ACCEPTANCE_WORKBENCH_PATH = "/ui/engineering/owner-acceptance"
logger = logging.getLogger(__name__)


class OwnerAcceptanceProjectionReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerAcceptanceProjectionOutcome:
    decision: OwnerAcceptanceDecision
    target: ChangeImpactTarget
    result: AdvisoryCheckProjectionResult | None


@dataclass(frozen=True, slots=True)
class OwnerAcceptanceProjectionService:
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider
    github_app_token: GitHubAppTokenProvider | None
    public_origin: str | None
    api_request: GitHubApiRequest = github_api_request

    @contextmanager
    def lock_current(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
    ) -> Iterator[ChangeImpactTarget]:
        projection_lock_store = require_owner_acceptance_projection_lock_store(store)
        for attempt in range(3):
            _, candidate_target = self.resolve_current(store=store, target=target)
            with projection_lock_store.owner_acceptance_projection_lock(
                repository_id=candidate_target.repository_id,
                pull_request_number=candidate_target.pull_request_number,
            ):
                _, confirmed_target = self.resolve_current(store=store, target=target)
                if _projection_targets_share_lock(candidate_target, confirmed_target):
                    yield confirmed_target
                    return
            logger.warning(
                "Owner acceptance projection target identity changed while locking; retrying.",
                extra={"projection_lock_attempt": attempt + 1},
            )
        raise ValueError("Owner acceptance projection target identity changed repeatedly.")

    def resolve_current(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
    ) -> tuple[OwnerAcceptanceDecision, ChangeImpactTarget]:
        initial_evidence = self.repository_evidence_provider.resolve(target)
        decision = evaluate_owner_acceptance(
            store=store,
            target=target,
            repository_evidence_provider=self.repository_evidence_provider,
        )
        evidence = self.repository_evidence_provider.resolve(target)
        if initial_evidence.target != evidence.target:
            raise ValueError("Owner acceptance projection target changed during evaluation.")
        _validate_projection_target(decision, evidence.target)
        return decision, evidence.target

    def project_conservative_locked(
        self,
        *,
        lock_target: ChangeImpactTarget,
        exact_target: ChangeImpactTarget,
        source_event_id: str,
    ) -> AdvisoryCheckProjectionResult:
        _require_projection_lock_target(lock_target, exact_target)
        with self._installation_token(exact_target) as installation_token:
            return project_owner_acceptance_update_in_progress(
                target=exact_target,
                source_event_id=source_event_id,
                public_origin=self._public_origin(),
                installation_token=installation_token,
                api_request=self.api_request,
            )

    def project_reconciliation_required_locked(
        self,
        *,
        lock_target: ChangeImpactTarget,
        exact_target: ChangeImpactTarget,
        source_event_id: str,
    ) -> AdvisoryCheckProjectionResult:
        _require_projection_lock_target(lock_target, exact_target)
        with self._installation_token(exact_target) as installation_token:
            return project_owner_acceptance_reconciliation_required(
                target=exact_target,
                source_event_id=source_event_id,
                public_origin=self._public_origin(),
                installation_token=installation_token,
                api_request=self.api_request,
            )

    def project_event_write_failure_locked(
        self,
        *,
        lock_target: ChangeImpactTarget,
        exact_target: ChangeImpactTarget,
        source_event_id: str,
        persistence_outcome: OwnerAcceptanceEventPersistenceOutcome,
    ) -> AdvisoryCheckProjectionResult:
        _require_projection_lock_target(lock_target, exact_target)
        with self._installation_token(exact_target) as installation_token:
            return project_owner_acceptance_event_write_failure(
                target=exact_target,
                source_event_id=source_event_id,
                persistence_outcome=persistence_outcome,
                public_origin=self._public_origin(),
                installation_token=installation_token,
                api_request=self.api_request,
            )

    def reconcile_locked(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
        lock_target: ChangeImpactTarget,
        source_event_id: str,
    ) -> OwnerAcceptanceProjectionOutcome:
        attempted_target = lock_target
        try:
            for attempt in range(3):
                decision, resolved_target = self.resolve_current(store=store, target=target)
                _require_projection_lock_target(lock_target, resolved_target)
                attempted_target = resolved_target
                with self._installation_token(resolved_target) as installation_token:
                    result = project_owner_acceptance_decision(
                        decision=decision,
                        target=resolved_target,
                        public_origin=self._public_origin(),
                        installation_token=installation_token,
                        api_request=self.api_request,
                    )
                confirmed_decision, confirmed_target = self.resolve_current(
                    store=store,
                    target=target,
                )
                if confirmed_target == resolved_target and owner_acceptance_projection_sha256(
                    confirmed_decision
                ) == owner_acceptance_projection_sha256(decision):
                    return OwnerAcceptanceProjectionOutcome(
                        decision=confirmed_decision,
                        target=confirmed_target,
                        result=result,
                    )
                self.project_conservative_locked(
                    lock_target=lock_target,
                    exact_target=resolved_target,
                    source_event_id=source_event_id,
                )
                logger.warning(
                    "Owner acceptance changed during GitHub projection; retrying current state.",
                    extra={"projection_attempt": attempt + 1},
                )
            raise ValueError("Owner acceptance changed repeatedly during GitHub projection.")
        except Exception as error:
            try:
                self.restore_reconciliation_required_locked(
                    store=store,
                    target=target,
                    lock_target=lock_target,
                    exact_target=attempted_target,
                    source_event_id=source_event_id,
                )
            except Exception as restoration_error:
                combined_error = ExceptionGroup(
                    "Owner acceptance projection and reconciliation recovery both failed.",
                    [error, restoration_error],
                )
                raise OwnerAcceptanceProjectionReconciliationError(str(error)) from combined_error
            raise OwnerAcceptanceProjectionReconciliationError(str(error)) from error

    def restore_reconciliation_required_locked(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
        lock_target: ChangeImpactTarget,
        exact_target: ChangeImpactTarget,
        source_event_id: str,
    ) -> None:
        self.project_reconciliation_required_locked(
            lock_target=lock_target,
            exact_target=exact_target,
            source_event_id=source_event_id,
        )
        _, current_target = self.resolve_current(store=store, target=target)
        _require_projection_lock_target(lock_target, current_target)
        if current_target != exact_target:
            self.project_reconciliation_required_locked(
                lock_target=lock_target,
                exact_target=current_target,
                source_event_id=source_event_id,
            )

    def restore_event_write_failure_locked(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
        lock_target: ChangeImpactTarget,
        exact_target: ChangeImpactTarget,
        source_event_id: str,
        persistence_outcome: OwnerAcceptanceEventPersistenceOutcome,
    ) -> None:
        self.project_event_write_failure_locked(
            lock_target=lock_target,
            exact_target=exact_target,
            source_event_id=source_event_id,
            persistence_outcome=persistence_outcome,
        )
        _, current_target = self.resolve_current(store=store, target=target)
        _require_projection_lock_target(lock_target, current_target)
        if current_target != exact_target:
            self.project_event_write_failure_locked(
                lock_target=lock_target,
                exact_target=current_target,
                source_event_id=source_event_id,
                persistence_outcome=persistence_outcome,
            )

    def reconcile(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
        source_event_id: str,
    ) -> OwnerAcceptanceProjectionOutcome:
        with self.lock_current(store=store, target=target) as lock_target:
            return self.reconcile_locked(
                store=store,
                target=target,
                lock_target=lock_target,
                source_event_id=source_event_id,
            )

    def reconcile_if_required(
        self,
        *,
        store: object,
        target: ChangeImpactTargetReference,
        source_event_id: str,
    ) -> OwnerAcceptanceProjectionOutcome:
        with self.lock_current(store=store, target=target) as lock_target:
            decision, current_target = self.resolve_current(store=store, target=target)
            _require_projection_lock_target(lock_target, current_target)
            if decision.status == "not_required":
                return OwnerAcceptanceProjectionOutcome(
                    decision=decision,
                    target=current_target,
                    result=None,
                )
            return self.reconcile_locked(
                store=store,
                target=target,
                lock_target=lock_target,
                source_event_id=source_event_id,
            )

    @contextmanager
    def _installation_token(
        self,
        target: ChangeImpactTarget,
    ) -> Iterator[GitHubAppInstallationToken]:
        if self.github_app_token is None:
            raise ValueError("Owner acceptance projection identity is unavailable.")
        installation_token = self.github_app_token(
            target.repository,
            target.repository_id,
        )
        try:
            yield installation_token
        finally:
            revoke_installation_token(
                installation_token=installation_token,
                api_request=self.api_request,
            )

    def _public_origin(self) -> str:
        if self.public_origin is None:
            raise ValueError("Owner acceptance projection public origin is unavailable.")
        return self.public_origin


def _validate_projection_target(
    decision: OwnerAcceptanceDecision,
    target: ChangeImpactTarget,
) -> None:
    bindings = tuple(
        product.binding for product in decision.products if product.binding is not None
    )
    if decision.binding is not None:
        bindings = (decision.binding, *bindings)
    for binding in bindings:
        if (
            binding.repository.casefold() != target.repository.casefold()
            or binding.repository_id != target.repository_id
            or binding.repository_owner_id != target.repository_owner_id
            or binding.pull_request_number != target.pull_request_number
            or binding.head_sha != target.head_sha
            or binding.tree_sha != target.tree_sha
        ):
            raise ValueError("Owner acceptance projection target changed during evaluation.")


def _projection_targets_share_lock(
    first: ChangeImpactTarget,
    second: ChangeImpactTarget,
) -> bool:
    return (
        first.repository_id == second.repository_id
        and first.pull_request_number == second.pull_request_number
    )


def _require_projection_lock_target(
    lock_target: ChangeImpactTarget,
    exact_target: ChangeImpactTarget,
) -> None:
    if not _projection_targets_share_lock(lock_target, exact_target):
        raise ValueError("Owner acceptance projection target identity changed.")


def project_owner_acceptance_decision(
    *,
    decision: OwnerAcceptanceDecision,
    target: ChangeImpactTarget,
    public_origin: str,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    external_id = owner_acceptance_projection_sha256(decision)
    details_url = owner_acceptance_workbench_url(
        public_origin=public_origin,
        target=target,
    )
    check_status, conclusion = _check_state(decision)
    return write_advisory_check_projection(
        projection=AdvisoryCheckProjection(
            name=OWNER_ACCEPTANCE_CHECK_NAME,
            repository=target.repository,
            repository_id=target.repository_id,
            head_sha=target.head_sha,
            external_id=external_id,
            details_url=details_url,
            title=f"Owner acceptance: {decision.status.replace('_', ' ')}",
            summary=_summary(decision),
            check_status=check_status,
            conclusion=conclusion,
        ),
        installation_token=installation_token,
        api_request=api_request,
    )


def project_owner_acceptance_update_in_progress(
    *,
    target: ChangeImpactTarget,
    source_event_id: str,
    public_origin: str,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    details_url = owner_acceptance_workbench_url(
        public_origin=public_origin,
        target=target,
    )
    return write_advisory_check_projection(
        projection=AdvisoryCheckProjection(
            name=OWNER_ACCEPTANCE_CHECK_NAME,
            repository=target.repository,
            repository_id=target.repository_id,
            head_sha=target.head_sha,
            external_id=owner_acceptance_update_projection_sha256(
                target=target,
                source_event_id=source_event_id,
            ),
            details_url=details_url,
            title="Owner acceptance: updating decision",
            summary=(
                "Launchplane is updating the authoritative Owner-review decision. "
                "GitHub success is intentionally withheld until the current decision "
                "is projected. Reconcile from the Launchplane Owner-review workbench "
                "if this check remains in progress."
            ),
            check_status="in_progress",
            conclusion=None,
        ),
        installation_token=installation_token,
        api_request=api_request,
    )


def project_owner_acceptance_reconciliation_required(
    *,
    target: ChangeImpactTarget,
    source_event_id: str,
    public_origin: str,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    details_url = owner_acceptance_workbench_url(
        public_origin=public_origin,
        target=target,
    )
    return write_advisory_check_projection(
        projection=AdvisoryCheckProjection(
            name=OWNER_ACCEPTANCE_CHECK_NAME,
            repository=target.repository,
            repository_id=target.repository_id,
            head_sha=target.head_sha,
            external_id=owner_acceptance_reconciliation_projection_sha256(
                target=target,
                source_event_id=source_event_id,
            ),
            details_url=details_url,
            title="Owner acceptance: reconciliation required",
            summary=(
                "Launchplane could not confirm the final authoritative Owner-review "
                "projection. Reconcile from the Launchplane Owner-review workbench "
                "before relying on this pull request state."
            ),
            conclusion="action_required",
        ),
        installation_token=installation_token,
        api_request=api_request,
    )


def project_owner_acceptance_event_write_failure(
    *,
    target: ChangeImpactTarget,
    source_event_id: str,
    persistence_outcome: OwnerAcceptanceEventPersistenceOutcome,
    public_origin: str,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    details_url = owner_acceptance_workbench_url(
        public_origin=public_origin,
        target=target,
    )
    if persistence_outcome == "absent":
        title = "Owner acceptance: update failed"
        summary = (
            "Launchplane could not persist the Owner-review event and confirmed that no "
            "event was recorded. Retry the action with the same Idempotency-Key from the "
            "Launchplane Owner-review workbench."
        )
    else:
        title = "Owner acceptance: write outcome unknown"
        summary = (
            "Launchplane could not determine whether the Owner-review event was persisted. "
            "Reconcile from the Launchplane Owner-review workbench before relying on this "
            "pull request state or retrying the action."
        )
    return write_advisory_check_projection(
        projection=AdvisoryCheckProjection(
            name=OWNER_ACCEPTANCE_CHECK_NAME,
            repository=target.repository,
            repository_id=target.repository_id,
            head_sha=target.head_sha,
            external_id=owner_acceptance_event_write_failure_projection_sha256(
                target=target,
                source_event_id=source_event_id,
                persistence_outcome=persistence_outcome,
            ),
            details_url=details_url,
            title=title,
            summary=summary,
            conclusion="failure",
        ),
        installation_token=installation_token,
        api_request=api_request,
    )


def owner_acceptance_workbench_url(
    *,
    public_origin: str,
    target: ChangeImpactTarget,
) -> str:
    return owner_acceptance_workbench_reference_url(
        public_origin=public_origin,
        repository=target.repository,
        pull_request_number=target.pull_request_number,
    )


def owner_acceptance_workbench_reference_url(
    *,
    public_origin: str,
    repository: str,
    pull_request_number: int,
) -> str:
    origin = public_origin.strip()
    try:
        parsed_origin = urlsplit(origin)
    except ValueError as error:
        raise ValueError(
            "Owner acceptance projection requires a valid browser public origin."
        ) from error
    if (
        parsed_origin.scheme not in {"http", "https"}
        or not parsed_origin.netloc
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        raise ValueError("Owner acceptance projection requires a valid browser public origin.")
    try:
        if parsed_origin.port is not None and not 1 <= parsed_origin.port <= 65535:
            raise ValueError
    except ValueError as error:
        raise ValueError(
            "Owner acceptance projection requires a valid browser public origin."
        ) from error
    if any(character.isspace() or ord(character) < 32 for character in origin):
        raise ValueError("Owner acceptance projection requires a valid browser public origin.")
    if repository.count("/") != 1 or any(
        not part or part != part.strip() for part in repository.split("/", 1)
    ):
        raise ValueError("Owner acceptance projection requires a valid repository target.")
    if pull_request_number < 1:
        raise ValueError("Owner acceptance projection requires a positive pull request number.")
    return (
        f"{origin.rstrip('/')}{OWNER_ACCEPTANCE_WORKBENCH_PATH}"
        f"?repository={quote(repository, safe='')}&pull_request={pull_request_number}"
    )


def owner_acceptance_projection_sha256(decision: OwnerAcceptanceDecision) -> str:
    payload = decision.model_dump(
        mode="json",
        exclude={"evaluated_at"},
        exclude_none=True,
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def owner_acceptance_update_projection_sha256(
    *,
    target: ChangeImpactTarget,
    source_event_id: str,
) -> str:
    payload = {
        "kind": "owner_acceptance_update_in_progress",
        "repository": target.repository,
        "repository_id": target.repository_id,
        "pull_request_number": target.pull_request_number,
        "head_sha": target.head_sha,
        "tree_sha": target.tree_sha,
        "source_event_id": source_event_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def owner_acceptance_reconciliation_projection_sha256(
    *,
    target: ChangeImpactTarget,
    source_event_id: str,
) -> str:
    payload = {
        "kind": "owner_acceptance_reconciliation_required",
        "repository": target.repository,
        "repository_id": target.repository_id,
        "pull_request_number": target.pull_request_number,
        "head_sha": target.head_sha,
        "tree_sha": target.tree_sha,
        "source_event_id": source_event_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def owner_acceptance_event_write_failure_projection_sha256(
    *,
    target: ChangeImpactTarget,
    source_event_id: str,
    persistence_outcome: OwnerAcceptanceEventPersistenceOutcome,
) -> str:
    payload = {
        "kind": "owner_acceptance_event_write_failure",
        "persistence_outcome": persistence_outcome,
        "repository": target.repository,
        "repository_id": target.repository_id,
        "pull_request_number": target.pull_request_number,
        "head_sha": target.head_sha,
        "tree_sha": target.tree_sha,
        "source_event_id": source_event_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _summary(decision: OwnerAcceptanceDecision) -> str:
    lines = [
        "Launchplane is the authoritative Owner-review system. This GitHub check is a "
        "routing and status projection only; record decisions in Launchplane.",
        "",
        f"Aggregate decision: **{decision.status.replace('_', ' ')}**",
        f"Reason code: `{decision.reason_code}`",
    ]
    if decision.products:
        lines.extend(("", "Per-product decisions:"))
        for product in decision.products:
            binding = (
                product.binding.binding_sha256 if product.binding is not None else "unavailable"
            )
            lines.append(
                f"- `{product.product}`: **{product.status.replace('_', ' ')}** "
                f"(`{product.reason_code}`; binding `{binding}`)"
            )
    elif decision.status == "not_required":
        lines.extend(("", "No product-specific Owner decision is currently required."))
    else:
        lines.extend(
            (
                "",
                "No product-specific Owner decision is available from the current "
                "authoritative evidence.",
            )
        )
    return "\n".join(lines)


def _check_state(
    decision: OwnerAcceptanceDecision,
) -> tuple[AdvisoryCheckRunStatus, AdvisoryCheckConclusion | None]:
    if decision.status == "pending":
        return "in_progress", None
    if decision.status in {"accepted", "not_required"}:
        return "completed", "success"
    if decision.status == "unavailable":
        return "completed", "failure"
    return "completed", "action_required"
