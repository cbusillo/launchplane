from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from control_plane.client_release import ClientReleaseRunView
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.merge_train_planning_scope import planning_scope_resolver
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.release_review import ReleaseReviewStore, release_version
from control_plane.release_waits_github import (
    released_change_contains,
    resolve_repository_release_waits,
)


def settle_product_release_waits(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    run: ClientReleaseRunView,
    scan_cutoffs: dict[str, str] | None = None,
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
    resolve_scope = planning_scope_resolver(
        policy=policy,
        source_repository=profile.repository,
        authorize=lambda target: target.service_authz == source.service_authz,
        resolve_token=lambda target: resolve_merge_train_github_token(
            source=target.github_token,
            repository=target.repository,
            control_plane_root=control_plane_root,
            raise_resolution_errors=True,
        ),
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
    scan_started = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
    updated_since = scan_cutoffs.get(decision.record_id, "") if scan_cutoffs is not None else ""
    failures = 0
    for repository in sorted(repositories):
        scope = source_scope if repository == profile.repository else resolve_scope(repository)
        try:
            resolve_repository_release_waits(
                scope=scope,
                product=profile.product,
                evidence=f"Release reached production: {decision.release_issue_url} (decision {decision.record_id}).",
                contains_commit=contains_commit,
                release_still_current=release_still_current,
                updated_since=updated_since,
            )
        except ValueError:
            failures += 1
    if failures:
        raise ValueError(f"Release wait resolution incomplete in {failures} repositories.")

    if scan_cutoffs is not None:
        scan_cutoffs[decision.record_id] = scan_started
