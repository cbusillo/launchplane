from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial
import logging
from time import monotonic
from pathlib import Path

from control_plane.client_release import ClientReleaseRunView
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.github_app_installation_token import GitHubAppInstallationToken
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.merge_train_planning_scope import planning_scope_resolver, PlanningScopeReview
from control_plane.merge_train_planning_completion import PlanningCompletionReview
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.release_review import ReleaseReviewStore, release_version
from control_plane.release_waits_github import (
    confirm_planning_poster,
    released_change_contains,
    resolve_repository_release_waits,
)


RELEASE_WAIT_RECHECK_SECONDS = 1800
_LOGGER = logging.getLogger(__name__)


def advance_product_release_waits(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    next_checks: dict[str, tuple[str, float]] | None,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    from control_plane.client_release import read_client_release_run

    if should_stop():
        return
    production = release_version(store=store, profile=profile, instance="prod")
    fingerprint = production.model_dump_json()
    if next_checks is not None:
        previous, next_read = next_checks.get(profile.product, ("", 0))
        if previous == fingerprint and monotonic() < next_read:
            return
        next_checks[profile.product] = (fingerprint, monotonic() + RELEASE_WAIT_RECHECK_SECONDS)
    decisions = [
        decision
        for decision in store.list_release_review_decision_records(product=profile.product)
        if decision.decision == "accepted"
        and decision.release_start in {"promote", "promote_with_rollback_drill"}
        and decision.release_issue_url
        and decision.checklist.repository == profile.repository
        and decision.checklist.candidate == production
    ]
    for decision in sorted(
        decisions, key=lambda item: datetime.fromisoformat(item.decided_at), reverse=True
    ):
        run = read_client_release_run(store=store, profile=profile, decision=decision)
        if run is not None and run.state == "passed":
            settle_product_release_waits(
                store=store,
                control_plane_root=control_plane_root,
                profile=profile,
                decision=decision,
                run=run,
                should_stop=should_stop,
            )
            return


def settle_product_release_waits(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    run: ClientReleaseRunView,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    if (
        run.state != "passed"
        or decision.decision != "accepted"
        or decision.release_start not in {"promote", "promote_with_rollback_drill"}
        or run.decision_record_id != decision.record_id
    ):
        return

    from control_plane.client_release import read_client_release_run

    def release_still_current() -> bool:
        return (
            store.read_product_profile_record(profile.product) == profile
            and store.read_release_review_decision_record(
                product=profile.product, record_id=decision.record_id
            )
            == decision
            and read_client_release_run(store=store, profile=profile, decision=decision) == run
            and release_version(store=store, profile=profile, instance="prod")
            == decision.checklist.candidate
        )

    if not release_still_current():
        return
    policy = resolve_merge_train_policy_record(store).policy
    source = policy.find_repository_policy(
        repository=profile.repository, base_branch=profile.default_branch
    )
    issued: dict[str, GitHubAppInstallationToken] = {}

    def planning_token(target: MergeTrainRepositoryPolicy) -> str:
        token = resolve_merge_train_github_token(
            source=target.github_token,
            repository=target.repository,
            control_plane_root=control_plane_root,
            raise_resolution_errors=True,
            include_permissions=True,
        )
        if not isinstance(token, GitHubAppInstallationToken):
            raise PlanningScopeReview("Verified planning credential identity is unavailable.")
        issued[target.repository] = token
        return token.token

    resolve_scope = planning_scope_resolver(
        policy=policy,
        source_repository=profile.repository,
        authorize=lambda target: target.service_authz == source.service_authz,
        resolve_token=planning_token,
    )
    source_scope = resolve_scope(profile.repository)
    included: dict[str, bool] = {}

    def contains_commit(commit: str) -> bool:
        if commit not in included:
            included[commit] = released_change_contains(
                scope=source_scope,
                required_commit=commit,
                released_commit=decision.checklist.candidate.source_commit,
            )
        return included[commit]

    repositories = {
        target.repository
        for target in policy.policies
        if target.repository.split("/", 1)[0].casefold()
        == profile.repository.split("/", 1)[0].casefold()
        and target.service_authz == source.service_authz
    }
    failures = 0
    for repository in sorted(repositories):
        if should_stop():
            return
        try:
            scope = source_scope if repository == profile.repository else resolve_scope(repository)
            resolve_repository_release_waits(
                scope=scope,
                product=profile.product,
                evidence=f"Release reached production: {decision.release_issue_url} (decision {decision.record_id}).",
                contains_commit=contains_commit,
                release_still_current=release_still_current,
                should_stop=should_stop,
                ensure_posting_author=partial(
                    confirm_planning_poster, issued.get(repository), scope.trusted_author_ids
                ),
            )
        except (ValueError, PlanningScopeReview, PlanningCompletionReview) as error:
            failures += 1
            _LOGGER.warning(
                "release wait scope incomplete repository=%s error_type=%s",
                repository,
                type(error).__name__,
            )
    if failures:
        raise ValueError(f"Release wait resolution incomplete in {failures} repositories.")
