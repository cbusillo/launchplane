from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from github_plan_status import wait_records

from control_plane.client_release import ClientReleaseRunView
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.github_payload import github_app_authored
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.release_review import ReleaseReviewStore, release_version
from control_plane.workflows.launchplane import github_api_request, launchplane_github_token


def _pages(path: str, token: str) -> list[dict[str, Any]]:
    records = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 11):
        response = github_api_request(
            path=f"{path}{separator}per_page=100&page={page}", token=token
        )
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise ValueError("Release wait inventory is incomplete.")
        records.extend(response)
        if len(response) < 100:
            return records
    raise ValueError("Release wait inventory exceeds its coverage bound.")


def resolve_repository_release_waits(
    *,
    repository: str,
    product: str,
    released_commit: str,
    evidence: str,
    token: str,
    app_id: int,
    trusted_author_ids: tuple[int, ...],
    reconcile_status: Callable[[int, tuple[int, ...]], object],
    release_still_current: Callable[[], bool],
) -> None:
    def trusted(source: dict[str, Any]) -> bool:
        return wait_records.author_is_trusted(
            source, repository.split("/", 1)[0], author_ids=trusted_author_ids
        ) or github_app_authored(
            source,
            app_id,
            lookup_app=lambda slug: github_api_request(path=f"/apps/{quote(slug)}", token=token),
        )

    def reconcile(number: int, comments: list[dict[str, Any]]) -> None:
        receipt_authors = tuple(
            {
                comment["user"]["id"]
                for comment in comments
                if evidence in str(comment.get("body", ""))
                and github_app_authored(comment, app_id)
                and isinstance(comment.get("user"), dict)
                and type(comment["user"].get("id")) is int
            }
        )
        reconcile_status(number, tuple(set(trusted_author_ids) | set(receipt_authors)))

    included: dict[str, bool] = {}

    def contains_commit(commit: str) -> bool:
        if commit == released_commit:
            return True
        if commit not in included:
            comparison = github_api_request(
                path=f"/repos/{repository}/compare/{commit}...{released_commit}", token=token
            )
            if not isinstance(comparison, dict) or comparison.get("status") not in {
                "ahead",
                "behind",
                "identical",
                "diverged",
            }:
                raise ValueError("Released source ancestry is unavailable.")
            included[commit] = comparison["status"] in {"ahead", "identical"}
        return included[commit]

    path = f"/repos/{repository}/issues"
    for issue in _pages(path + "?state=open", token):
        if "pull_request" in issue:
            continue
        number = issue.get("number")
        if type(number) is not int or number < 1:
            raise ValueError("Release wait issue identity is unavailable.")
        destination = f"{path}/{number}"
        comments = _pages(destination + "/comments", token)
        record = wait_records.current(issue, comments, trusted)
        resolved, actions = wait_records.resolve_release(
            record, product=product, contains_commit=contains_commit, evidence=evidence
        )
        if not actions:
            if any(
                evidence in str(comment.get("body", "")) and trusted(comment)
                for comment in comments
            ):
                reconcile(number, comments)
            continue
        fresh_issue = github_api_request(path=destination, token=token)
        fresh_comments = _pages(destination + "/comments", token)
        if fresh_issue != issue or fresh_comments != comments or not release_still_current():
            raise ValueError("Release wait changed before completion; preserve the newer record.")
        body = "\n\n".join([evidence, *actions]).replace("@", "@\u200b")
        body += "\n\n" + wait_records.marker(resolved)
        result = github_api_request(
            path=destination + "/comments", token=token, method="POST", body={"body": body}
        )
        if not isinstance(result, dict) or type(result.get("id")) is not int:
            raise ValueError("Release wait resolution was not confirmed.")
        observed = wait_records.current(
            fresh_issue, _pages(destination + "/comments", token), trusted
        )
        if observed != resolved:
            raise ValueError("Release wait readback changed; preserve the newer record.")
        reconcile(number, [result])


def settle_product_release_waits(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    run: ClientReleaseRunView,
) -> None:
    if (
        run.state != "passed"
        or decision.decision != "accepted"
        or decision.release_start not in {"promote", "promote_with_rollback_drill"}
        or run.decision_record_id != decision.record_id
    ):
        return
    if (
        release_version(store=store, profile=profile, instance="prod")
        != decision.checklist.candidate
    ):
        return
    from control_plane.client_release import read_client_release_run
    from control_plane.merge_train_planning_completion import reconcile_issue_status
    from control_plane.merge_train_planning_scope import PlanningRepositoryScope

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

    policy = resolve_merge_train_policy_record(store).policy
    trusted_ids = tuple(
        {
            identity
            for target in policy.policies
            if target.repository.casefold() == profile.repository.casefold()
            for identity in target.enqueue.trusted_automation_github_user_ids
        }
    )
    context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
    with launchplane_github_token(
        control_plane_root=control_plane_root,
        context_name=context,
        repository=profile.repository,
        purpose="source_issue_feedback",
    ) as token:
        if not token:
            raise ValueError("Release wait source-control access is unavailable.")
        resolve_repository_release_waits(
            repository=profile.repository,
            product=profile.product,
            released_commit=decision.checklist.candidate.source_commit,
            evidence=f"Release reached production: {decision.release_issue_url} (decision {decision.record_id}).",
            token=token,
            app_id=resolve_delivery_github_app_id(control_plane_root=control_plane_root),
            trusted_author_ids=trusted_ids,
            release_still_current=release_still_current,
            reconcile_status=lambda number, author_ids: reconcile_issue_status(
                scope=PlanningRepositoryScope(
                    profile.repository, profile.default_branch, token, author_ids
                ),
                number=number,
            ),
        )
