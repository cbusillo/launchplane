"""Project a saved release decision into a durable tenant repository issue."""

from datetime import datetime
from pathlib import Path
import re
from urllib.parse import quote

from control_plane.github_payload import github_app_authored
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id
from control_plane.service_human_auth import launchplane_public_origin_from_env

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.release_review import ReleaseReviewStore
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
    launchplane_github_token,
)


def _literal(value: str) -> str:
    # Notes and reasons are data, including Markdown fences and incidental mentions.
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    return f"{fence}\n{value.replace('@', '@\u200b')}\n{fence}"


def release_decision_marker(record_id: str) -> str:
    return f"<!-- launchplane:release-decision:{record_id} -->"


def _temporary_hotfix_notice(*, client_login: str, review_url: str) -> list[str]:
    """The Client's notice: plain words, the reason, and where to accept or reject."""

    mention = f"@{client_login}" if client_login else "The Client"
    return [
        f"{mention}, the Director is putting an urgent temporary fix live now, before your review, "
        "because of breakage on the live site. The reason is below.",
        "Once it is live, please review it like any release. **Accept** keeps it as your "
        "accepted release. **Request changes** rolls the site back to your last accepted release.",
        f"Review it here: {review_url}"
        if review_url
        else "Review it on the Launchplane release page.",
        "Editing this issue does not change the decision.",
    ]


def _decision_effect(decision: ReleaseReviewDecisionRecord) -> str:
    """Describe the saved action, without deriving policy from today's profile."""

    if decision.release_start == "rollback":
        return (
            "The Client requested changes to a live temporary hotfix. Launchplane rolls "
            "the site back to their last accepted release."
        )
    if decision.release_start in ("promote", "promote_with_rollback_drill"):
        effect = (
            "This acceptance starts Launchplane's gated production release: a verified "
            "backup, promotion of the reviewed version, post-deploy checks, and automatic "
            "rollback if the release fails."
        )
        if decision.release_start == "promote_with_rollback_drill":
            effect += (
                " This release also runs the rollback drill: return to the reviewed "
                "previous production version, take another verified backup, and promote "
                "the same reviewed version again."
            )
        return effect
    if decision.decision == "changes_requested":
        return "The Client requested changes. This decision withdraws approval and starts no production release."
    if decision.decision == "overridden":
        return "This records an admin override of approval. It starts no production release."
    return "This records acceptance only. This decision starts no production release."


def release_decision_issue_body(
    decision: ReleaseReviewDecisionRecord, *, client_login: str = "", review_url: str = ""
) -> str:
    checklist = decision.checklist
    hotfix = decision.decision == "temporary_hotfix"
    lines = [
        release_decision_marker(decision.record_id),
        "# Release decision",
        f"Decision: **{decision.decision.replace('_', ' ')}**",
        f"Recorded by `{decision.actor_github_login}` (GitHub ID `{decision.actor_github_id}`)",
        f"Recorded at: {decision.decided_at}",
        "Acceptance source: Director standing acceptance recorded in Client settings."
        if decision.acceptance_source == "director_standing"
        else "Decision source: signed-in human session.",
        "",
        *(
            _temporary_hotfix_notice(client_login=client_login, review_url=review_url)
            if hotfix
            else (
                _decision_effect(decision),
                "Editing this issue does not change Launchplane approval or the release action.",
            )
        ),
        "",
        "## Release",
        _literal(
            f"Product: {checklist.product}\n"
            f"Production: {checklist.production.source_commit} ({checklist.production.artifact_id})\n"
            f"Testing: {checklist.candidate.source_commit} ({checklist.candidate.artifact_id})\n"
            f"Production shared inputs: {checklist.production.shared_addons_digest}\n"
            f"Testing shared inputs: {checklist.candidate.shared_addons_digest}\n"
            + (f"Testing site: {checklist.testing_url}\n" if checklist.testing_url else "")
            + f"Checklist digest: {decision.checklist_digest}"
        ),
        "",
    ]
    if checklist.candidate.runtime_settings is not None:
        settings = checklist.candidate.runtime_settings
        production_settings = checklist.production.runtime_settings
        changed = {
            key
            for key, value in settings.values.items()
            if production_settings is None or production_settings.values.get(key) != value
        }
        removed = set(settings.absent_keys) - set(
            production_settings.absent_keys if production_settings else ()
        )
        if changed or removed:
            lines.extend(
                [
                    "## Production settings",
                    "This release applies its saved non-secret production settings. Rollback restores the previous settings with the previous artifact.",
                    _literal(
                        "Set: "
                        + ", ".join(sorted(changed))
                        + "\nRemove: "
                        + ", ".join(sorted(removed))
                    ),
                ]
            )
    lines.extend(["", "## Client checklist"])
    for item in checklist.items:
        lines.extend(
            [
                f"### Pull request #{item.pull_request_number}",
                _literal(item.title),
                item.url,
                "Accepted in preview." if item.already_reviewed else "Not accepted in preview.",
                _literal(item.owner_test_notes or "Client test notes are missing."),
            ]
        )
    if not checklist.items:
        lines.append(
            "No product-repository pull request changes. Shared changes are listed below."
            if checklist.shared_sources
            else "No merged pull request changes between these versions."
        )
    for source in checklist.shared_sources:
        lines.extend(
            [
                "## Shared release inputs",
                _literal(
                    f"Repository: {source.repository}\n"
                    f"Production: {source.production_commit}\n"
                    f"Testing: {source.candidate_commit}"
                ),
            ]
        )
        for item in source.items:
            lines.extend(
                [
                    f"### Pull request {source.repository}#{item.pull_request_number}",
                    _literal(item.title),
                    item.url,
                    _literal(item.owner_test_notes or "Client test notes are missing."),
                ]
            )
        if source.untracked_commits:
            lines.extend(
                [
                    "### Commits without pull request coverage",
                    _literal("\n".join(source.untracked_commits)),
                ]
            )
    if checklist.untracked_commits:
        lines.extend(
            [
                "## Commits without pull request coverage",
                _literal("\n".join(checklist.untracked_commits)),
            ]
        )
    if checklist.additional_changes:
        lines.extend(["## Additional changes", _literal("\n".join(checklist.additional_changes))])
    if decision.reason:
        lines.extend(["## Decision reason", _literal(decision.reason)])
    if decision.release_during_business_hours:
        lines.extend(
            [
                "## Release timing",
                "The signed-in human explicitly requested an urgent release during business hours. The acceptance and backup gates still apply.",
            ]
        )
    return "\n\n".join(lines)


def publish_release_decision(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> str:
    """Serialize lookup, creation and acknowledgement of a saved decision."""
    from control_plane.release_invitation import release_candidate_identity

    stored = store.read_release_review_decision_record(
        product=decision.product, record_id=decision.record_id
    )
    if stored.model_copy(update={"release_issue_url": ""}) != decision.model_copy(
        update={"release_issue_url": ""}
    ):
        raise ValueError("The saved release decision has changed.")
    if profile.product != stored.product or profile.repository != stored.checklist.repository:
        raise ValueError("The release record must belong to the product repository.")
    if stored.release_issue_url:
        return stored.release_issue_url
    previous: ReleaseReviewDecisionRecord | None = None
    for item in store.list_release_review_decision_records(product=profile.product):
        if item.record_id == stored.record_id or item.checklist_digest != stored.checklist_digest:
            continue
        previous_key = (datetime.fromisoformat(item.decided_at), item.record_id)
        if previous_key >= (datetime.fromisoformat(stored.decided_at), stored.record_id):
            continue
        if previous is None or previous_key > (
            datetime.fromisoformat(previous.decided_at),
            previous.record_id,
        ):
            previous = item
    identity = release_candidate_identity(profile, stored.checklist_digest)
    with store.release_review_publication_lock(record_id=f"candidate:{profile.product}:{identity}"):
        issue_url = _publish_release_decision_issue(
            control_plane_root=control_plane_root,
            profile=profile,
            decision=stored,
            hotfix_review=previous is not None and previous.decision == "temporary_hotfix",
            previous=previous,
        )
    with store.release_review_publication_lock(record_id=stored.record_id):
        published = store.record_release_review_decision_publication(
            record_id=stored.record_id, release_issue_url=issue_url
        )
        return published.release_issue_url


def _publish_release_decision_issue(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    hotfix_review: bool,
    previous: ReleaseReviewDecisionRecord | None,
) -> str:
    origin = launchplane_public_origin_from_env()
    body = release_decision_issue_body(
        decision,
        client_login=profile.owner.github_login,
        review_url=f"{origin}/ui/owner-review?product={quote(profile.product, safe='')}"
        if origin
        else "",
    )
    marker = release_decision_marker(decision.record_id)
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    with launchplane_github_token(
        token_resolver=resolve_launchplane_github_token,
        api_request=github_api_request,
        control_plane_root=control_plane_root,
        context_name=lane.context,
        repository=profile.repository,
        purpose="release_record",
    ) as token:
        if not token:
            raise ValueError("Release record source-control access is unavailable.")
        path = f"/repos/{quote(profile.repository)}/issues"
        from control_plane.release_invitation import (
            _receipt_lines,
            _release_issue_number,
            release_candidate_issue_marker,
            release_issue_product_marker,
        )

        from control_plane.release_invitation import release_candidate_identity

        identity = release_candidate_identity(profile, decision.checklist_digest)
        candidate_marker = release_candidate_issue_marker(identity)
        product_marker = release_issue_product_marker(profile.product)
        app_id = resolve_delivery_github_app_id(control_plane_root=control_plane_root)
        request = profile.release_request
        if request is not None and request.checklist_digest != decision.checklist_digest:
            request = None
        cutoff = datetime.fromisoformat(
            request.decided_at
            if request
            else previous.decided_at
            if previous
            else decision.decided_at
        ).replace(microsecond=0)
        issue = None
        if (
            previous is not None
            and previous.release_issue_url
            and (
                request is None
                or (
                    previous.record_id != request.client_decision_record_id
                    and datetime.fromisoformat(previous.decided_at)
                    >= datetime.fromisoformat(request.decided_at)
                )
            )
        ):
            match = re.fullmatch(
                rf"https://github\.com/{re.escape(profile.repository)}/issues/([1-9][0-9]*)",
                previous.release_issue_url,
            )
            if match is None:
                raise ValueError("Release record destination does not match its repository.")
            issue = github_api_request(path=f"{path}/{match[1]}", token=token)
            if (
                not isinstance(issue, dict)
                or "pull_request" in issue
                or not isinstance(issue.get("body"), str)
            ):
                raise ValueError("Release record lookup is incomplete.")
        else:
            for page in range(1, 11):
                issues = github_api_request(
                    path=f"{path}?state=all&sort=created&direction=desc&per_page=100&page={page}",
                    token=token,
                )
                if not isinstance(issues, list):
                    raise ValueError("Release record lookup is unavailable.")
                reached_older_issue = False
                for item in issues:
                    if not isinstance(item, dict):
                        raise ValueError("Release record lookup is incomplete.")
                    created_at = item.get("created_at")
                    if isinstance(created_at, str):
                        reached_older_issue |= datetime.fromisoformat(created_at) < cutoff
                    if "pull_request" in item or not isinstance(item.get("body"), str):
                        continue
                    lines = _receipt_lines(item)
                    if lines[:1] == [marker]:
                        issue = item
                        break
                    if (
                        lines[:1] == [candidate_marker]
                        and product_marker in lines
                        and github_app_authored(
                            item,
                            app_id,
                            lookup_app=lambda slug: github_api_request(
                                path=f"/apps/{quote(slug)}", token=token
                            ),
                        )
                    ):
                        issue = item
                        break
                if issue is not None or len(issues) < 100 or reached_older_issue:
                    break
            else:
                raise ValueError("Release record lookup exceeds the supported size.")
        closes_review = not decision.release_start and (
            decision.decision == "changes_requested"
            or (hotfix_review and decision.decision == "accepted")
        )
        if issue is None:
            issue_body = f"{candidate_marker}\n{product_marker}\n\n{body}"
            issue = github_api_request(
                path=path,
                token=token,
                method="POST",
                body={
                    "title": f"Release {decision.checklist.candidate.source_commit[:7]}: "
                    + (
                        "urgent temporary fix going live, your review needed"
                        if decision.decision == "temporary_hotfix"
                        else decision.decision.replace("_", " ")
                    ),
                    "body": issue_body,
                },
            )
            if not isinstance(issue, dict):
                raise ValueError("Release record creation was not confirmed.")
            issue = {"body": issue_body, "state": "open", **issue}
        if not isinstance(issue, dict):
            raise ValueError("Release record lookup is incomplete.")
        number = _release_issue_number(issue)
        if marker not in _receipt_lines(issue):
            comments = []
            for page in range(1, 11):
                result = github_api_request(
                    path=f"{path}/{number}/comments?per_page=100&page={page}", token=token
                )
                if not isinstance(result, list) or any(
                    not isinstance(comment, dict) for comment in result
                ):
                    raise ValueError("Release decision lookup is incomplete.")
                comments.extend(result)
                if (
                    any(
                        marker in _receipt_lines(comment)
                        and github_app_authored(
                            comment,
                            app_id,
                            lookup_app=lambda slug: github_api_request(
                                path=f"/apps/{quote(slug)}", token=token
                            ),
                        )
                        for comment in result
                    )
                    or len(result) < 100
                ):
                    break
            else:
                raise ValueError("Release decision lookup exceeds the supported size.")
            if not any(
                marker in _receipt_lines(comment)
                and github_app_authored(
                    comment,
                    app_id,
                    lookup_app=lambda slug: github_api_request(
                        path=f"/apps/{quote(slug)}", token=token
                    ),
                )
                for comment in comments
            ):
                published = github_api_request(
                    path=f"{path}/{number}/comments",
                    token=token,
                    method="POST",
                    body={"body": body},
                )
                if not isinstance(published, dict) or not isinstance(published.get("id"), int):
                    raise ValueError("Release decision publication was not confirmed.")
        state = "closed" if closes_review else "open" if decision.release_start else ""
        if state and issue.get("state") != state:
            result = github_api_request(
                path=f"{path}/{number}", token=token, method="PATCH", body={"state": state}
            )
            if (
                not isinstance(result, dict)
                or result.get("number") != number
                or result.get("state") != state
            ):
                raise ValueError("Release decision state was not confirmed.")
        return f"https://github.com/{profile.repository}/issues/{number}"
