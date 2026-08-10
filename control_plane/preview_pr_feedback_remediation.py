import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Literal, Protocol, cast
from urllib.parse import quote

import click

from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackRecord,
    build_preview_pr_feedback_id,
)
from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackMutationEvidence,
    PreviewPrFeedbackObservation,
    PreviewPrFeedbackRemediationAction,
    PreviewPrFeedbackRemediationRecord,
    PreviewPrFeedbackRemediationRequest,
    build_preview_pr_feedback_remediation_id,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.launchplane import (
    delete_github_issue_comment,
    github_api_request,
    github_pull_request_reference,
    resolve_launchplane_github_token,
    update_github_issue_comment,
)
from control_plane.workflows.preview_pr_feedback import (
    DEFAULT_PREVIEW_FEEDBACK_MARKER,
    LEGACY_PREVIEW_FEEDBACK_MARKERS,
    render_preview_pr_feedback_markdown,
)


MANAGED_PREVIEW_FEEDBACK_MARKERS = (
    DEFAULT_PREVIEW_FEEDBACK_MARKER,
    *LEGACY_PREVIEW_FEEDBACK_MARKERS,
)
MAX_COMMENT_SCAN_PAGES = 10
GitHubApiRequest = Callable[..., object]


class PreviewPrFeedbackRemediationStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_preview_pr_feedback_remediation_records(
        self,
        *,
        actor: str = "",
        idempotency_key: str = "",
        mode: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRemediationRecord, ...]: ...


@dataclass(frozen=True)
class BoundPreviewPrFeedbackTarget:
    profile: LaunchplaneProductProfileRecord
    owner: str
    repo: str
    pr_number: int


class PreviewPrFeedbackRemediationApplyError(click.ClickException):
    def __init__(
        self,
        message: str,
        *,
        mutation_evidence: PreviewPrFeedbackMutationEvidence,
    ) -> None:
        super().__init__(message)
        self.mutation_evidence = mutation_evidence


def bind_preview_pr_feedback_target(
    *,
    record_store: PreviewPrFeedbackRemediationStore,
    request: PreviewPrFeedbackRemediationRequest,
) -> BoundPreviewPrFeedbackTarget:
    profile = record_store.read_product_profile_record(request.product)
    if not profile.preview.enabled or not profile.preview.context.strip():
        raise ValueError("Product profile does not define an enabled preview context.")
    if request.context != profile.preview.context.strip():
        raise ValueError("Preview feedback remediation context does not match product profile.")
    if request.repository.casefold() != profile.repository.strip().casefold():
        raise ValueError("Preview feedback remediation repository does not match product profile.")
    reference = github_pull_request_reference(pr_url=request.pull_request_url)
    if reference is None:
        raise ValueError("Preview feedback remediation requires a GitHub pull request URL.")
    if not request.pull_request_url.casefold().startswith("https://github.com/"):
        raise ValueError("Preview feedback remediation requires an HTTPS GitHub pull request URL.")
    referenced_repository = f"{reference['owner']}/{reference['repo']}"
    if referenced_repository.casefold() != request.repository.casefold():
        raise ValueError("Preview feedback remediation PR URL does not match repository.")
    return BoundPreviewPrFeedbackTarget(
        profile=profile,
        owner=reference["owner"],
        repo=reference["repo"],
        pr_number=reference["pr_number"],
    )


def _actor(payload: object) -> tuple[int, str]:
    if not isinstance(payload, dict):
        raise click.ClickException("GitHub authenticated user response must be an object.")
    actor_id = payload.get("id")
    login = payload.get("login")
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id < 1:
        raise click.ClickException("GitHub token identity is missing a stable actor id.")
    if not isinstance(login, str) or not login.strip():
        raise click.ClickException("GitHub token identity is missing a login.")
    return actor_id, login.strip()


def _comment_actor(comment: dict[str, object]) -> tuple[int, str]:
    user = comment.get("user")
    if not isinstance(user, dict):
        return 0, ""
    actor_id = user.get("id")
    login = user.get("login")
    return (
        actor_id if isinstance(actor_id, int) and not isinstance(actor_id, bool) else 0,
        login.strip() if isinstance(login, str) else "",
    )


def _observation_digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def observe_managed_preview_pr_feedback(
    *,
    target: BoundPreviewPrFeedbackTarget,
    token: str,
    api_request: GitHubApiRequest = github_api_request,
) -> PreviewPrFeedbackObservation:
    token_actor_id, token_actor_login = _actor(api_request(path="/user", token=token))
    marker_comments: list[tuple[dict[str, object], str]] = []
    page = 1
    while True:
        payload = api_request(
            path=(
                f"/repos/{quote(target.owner)}/{quote(target.repo)}/issues/"
                f"{target.pr_number}/comments?per_page=100&page={page}"
            ),
            token=token,
        )
        if not isinstance(payload, list):
            raise click.ClickException("GitHub issue comments response must be a list.")
        for item in payload:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            if not isinstance(body, str):
                continue
            matched_markers = [
                marker for marker in MANAGED_PREVIEW_FEEDBACK_MARKERS if marker in body
            ]
            if len(matched_markers) > 1:
                raise click.ClickException("Managed preview feedback marker match is ambiguous.")
            if matched_markers:
                marker_comments.append((cast(dict[str, object], item), matched_markers[0]))
        if len(payload) < 100:
            break
        if page >= MAX_COMMENT_SCAN_PAGES:
            raise click.ClickException(
                "Managed preview feedback comment scan exceeded its safe page limit."
            )
        page += 1
    foreign = [
        comment
        for comment, _marker in marker_comments
        if _comment_actor(comment)[0] != token_actor_id
    ]
    if foreign:
        raise click.ClickException(
            "Managed preview feedback marker is owned by a different GitHub actor."
        )
    if len(marker_comments) > 1:
        raise click.ClickException("Multiple owned managed preview feedback comments were found.")
    if not marker_comments:
        digest_payload = {"state": "absent", "token_actor_id": token_actor_id}
        return PreviewPrFeedbackObservation(
            state="absent",
            digest_sha256=_observation_digest(digest_payload),
            token_actor_id=token_actor_id,
            token_actor_login=token_actor_login,
        )
    comment, marker = marker_comments[0]
    comment_id = comment.get("id")
    body = comment.get("body")
    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1:
        raise click.ClickException("Managed preview feedback comment is missing a numeric id.")
    if not isinstance(body, str):
        raise click.ClickException("Managed preview feedback comment is missing its body.")
    author_id, author_login = _comment_actor(comment)
    comment_url = comment.get("html_url")
    normalized_url = comment_url.strip() if isinstance(comment_url, str) else ""
    digest_payload = {
        "state": "present",
        "marker": marker,
        "comment_id": comment_id,
        "comment_url": normalized_url,
        "comment_author_id": author_id,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "token_actor_id": token_actor_id,
    }
    return PreviewPrFeedbackObservation(
        state="present",
        digest_sha256=_observation_digest(digest_payload),
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        excerpt=body[:500],
        marker=marker,
        comment_id=comment_id,
        comment_url=normalized_url,
        comment_author_id=author_id,
        comment_author_login=author_login,
        token_actor_id=token_actor_id,
        token_actor_login=token_actor_login,
    )


def remediation_action(
    *, request: PreviewPrFeedbackRemediationRequest, observation: PreviewPrFeedbackObservation
) -> PreviewPrFeedbackRemediationAction:
    if observation.state == "absent":
        return "none"
    return "delete_comment" if request.terminal_status == "cleared" else "replace_comment"


def matching_dry_run(
    *,
    record_store: PreviewPrFeedbackRemediationStore,
    actor: str,
    idempotency_key: str,
    continuity_sha256: str,
) -> PreviewPrFeedbackRemediationRecord | None:
    for record in record_store.list_preview_pr_feedback_remediation_records(
        actor=actor,
        idempotency_key=idempotency_key,
        mode="dry-run",
        limit=1,
    ):
        if record.mode == "dry-run" and record.continuity_sha256 == continuity_sha256:
            return record
    return None


def build_remediation_record(
    *,
    request: PreviewPrFeedbackRemediationRequest,
    target: BoundPreviewPrFeedbackTarget,
    actor: str,
    trace_id: str,
    idempotency_key: str,
    requested_at: str,
    observation: PreviewPrFeedbackObservation,
    outcome: Literal["planned", "deleted", "replaced", "already_absent", "failed"] = "planned",
    mutation_evidence: PreviewPrFeedbackMutationEvidence | None = None,
    companion_feedback_id: str = "",
) -> PreviewPrFeedbackRemediationRecord:
    return PreviewPrFeedbackRemediationRecord(
        remediation_id=build_preview_pr_feedback_remediation_id(trace_id=trace_id),
        product=request.product,
        context=request.context,
        repository=request.repository,
        pull_request_url=request.pull_request_url,
        pull_request_number=target.pr_number,
        mode=request.mode,
        terminal_status=request.terminal_status,
        actor=actor,
        reason=request.reason,
        related_issue=request.related_issue,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        requested_at=requested_at,
        continuity_sha256=request.continuity_sha256,
        observation=observation,
        planned_action=remediation_action(request=request, observation=observation),
        outcome=outcome,
        mutation_evidence=mutation_evidence or PreviewPrFeedbackMutationEvidence(),
        companion_feedback_id=companion_feedback_id,
    )


def apply_remediation(
    *,
    request: PreviewPrFeedbackRemediationRequest,
    target: BoundPreviewPrFeedbackTarget,
    token: str,
    observation: PreviewPrFeedbackObservation,
    requested_at: str,
    api_request: GitHubApiRequest = github_api_request,
) -> tuple[
    Literal["deleted", "replaced", "already_absent"],
    PreviewPrFeedbackMutationEvidence,
    PreviewPrFeedbackRecord,
]:
    comment_markdown = render_preview_pr_feedback_markdown(
        marker=observation.marker or DEFAULT_PREVIEW_FEEDBACK_MARKER,
        status=request.terminal_status,
        anchor_pr_number=target.pr_number,
    )
    feedback_id = build_preview_pr_feedback_id(
        context_name=request.context,
        anchor_pr_number=target.pr_number,
        requested_at=requested_at,
    )
    outcome: Literal["deleted", "replaced", "already_absent"]
    if observation.state == "absent":
        outcome = "already_absent"
        evidence = PreviewPrFeedbackMutationEvidence(verified_absent=True)
        delivery_status = "skipped"
        delivery_action = "no_existing_comment"
        comment_id = 0
        comment_url = ""
    elif request.terminal_status == "cleared":
        evidence = PreviewPrFeedbackMutationEvidence(
            attempted=True,
            method="DELETE",
            comment_id=observation.comment_id,
            before_digest_sha256=observation.digest_sha256,
        )
        try:
            delete_github_issue_comment(
                owner=target.owner,
                repo=target.repo,
                comment_id=observation.comment_id,
                token=token,
            )
            evidence.mutated = True
            after = observe_managed_preview_pr_feedback(
                target=target, token=token, api_request=api_request
            )
            evidence.after_digest_sha256 = after.digest_sha256
            evidence.verified_absent = after.state == "absent"
        except click.ClickException as error:
            raise PreviewPrFeedbackRemediationApplyError(
                str(error), mutation_evidence=evidence
            ) from error
        if not evidence.verified_absent:
            raise PreviewPrFeedbackRemediationApplyError(
                "Managed preview feedback deletion could not be verified.",
                mutation_evidence=evidence,
            )
        outcome = "deleted"
        delivery_status = "delivered"
        delivery_action = "deleted_comment"
        comment_id = observation.comment_id
        comment_url = observation.comment_url
    else:
        evidence = PreviewPrFeedbackMutationEvidence(
            attempted=True,
            method="PATCH",
            comment_id=observation.comment_id,
            before_digest_sha256=observation.digest_sha256,
        )
        try:
            update_github_issue_comment(
                owner=target.owner,
                repo=target.repo,
                comment_id=observation.comment_id,
                token=token,
                body=comment_markdown,
            )
            evidence.mutated = True
            after = observe_managed_preview_pr_feedback(
                target=target, token=token, api_request=api_request
            )
            evidence.after_digest_sha256 = after.digest_sha256
        except click.ClickException as error:
            raise PreviewPrFeedbackRemediationApplyError(
                str(error), mutation_evidence=evidence
            ) from error
        expected_body_sha256 = hashlib.sha256(comment_markdown.encode("utf-8")).hexdigest()
        if (
            after.state != "present"
            or after.comment_id != observation.comment_id
            or after.body_sha256 != expected_body_sha256
        ):
            raise PreviewPrFeedbackRemediationApplyError(
                "Managed preview feedback replacement could not be verified.",
                mutation_evidence=evidence,
            )
        outcome = "replaced"
        delivery_status = "delivered"
        delivery_action = "updated_comment"
        comment_id = observation.comment_id
        comment_url = after.comment_url
    feedback = PreviewPrFeedbackRecord(
        feedback_id=feedback_id,
        product=request.product,
        context=request.context,
        source="operator-remediation",
        requested_at=requested_at,
        repository=request.repository,
        anchor_repo=target.repo,
        anchor_pr_number=target.pr_number,
        anchor_pr_url=request.pull_request_url,
        status=request.terminal_status,
        marker=observation.marker or DEFAULT_PREVIEW_FEEDBACK_MARKER,
        comment_markdown=comment_markdown,
        delivery_status=cast(Literal["delivered", "skipped", "failed"], delivery_status),
        delivery_action=delivery_action,
        comment_id=comment_id,
        comment_url=comment_url,
    )
    return outcome, evidence, feedback


def resolve_remediation_token(*, control_plane_root: Path, context: str) -> str:
    token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root, context_name=context
    )
    if not token:
        raise click.ClickException(
            "Launchplane runtime records do not expose GITHUB_TOKEN for this context."
        )
    return token
