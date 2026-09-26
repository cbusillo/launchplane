"""GitHub projection of immutable Owner decisions, including their complete prose."""

from collections.abc import Callable
import json
import re
from urllib.parse import quote

from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.github_payload import required_positive_int


def validate_owner_feedback_decision(decision: ProductReviewDecisionRecord) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", decision.record_id):
        raise ValueError("Owner feedback requires a safe decision identifier.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", decision.head_sha):
        raise ValueError("Owner feedback requires the reviewed commit.")


def owner_feedback_comment(decision: ProductReviewDecisionRecord, *, review_url: str) -> str:
    validate_owner_feedback_decision(decision)
    metadata = decision.model_dump(mode="json", exclude={"feedback_url", "schema_version"})
    metadata.update(schema_version=1, review_url=review_url)
    encoded = json.dumps(metadata, sort_keys=True)
    # Owner prose is data, even when it contains HTML comment terminators or mentions.
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    reason = decision.reason or "No additional notes."
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", reason)), default=0))
    literal_reason = reason.replace("@", "@\u200b")
    return "\n".join(
        (
            f"<!-- launchplane:product-review:{decision.record_id} -->",
            f"<!-- launchplane:owner-review {encoded} -->",
            "## Owner review from Launchplane",
            f"**{decision.decision.replace('_', ' ').capitalize()}** by "
            f"`{decision.owner_github_login}` (GitHub ID `{decision.owner_github_id}`).",
            f"Recorded at: {decision.decided_at}",
            f"Reviewed commit: `{decision.head_sha}`",
            f"[Decision in Launchplane]({review_url}) — record `{decision.record_id}`",
            "",
            "### Owner's feedback",
            fence,
            literal_reason,
            fence,
            "",
            "This decision applies only to the reviewed commit. Launchplane remains the "
            "decision authority; this comment does not authorize a merge or deployment.",
        )
    )


def publish_owner_feedback(
    *,
    decision: ProductReviewDecisionRecord,
    review_url: str,
    token: str,
    actor_id: int,
    api_request: Callable[..., object],
) -> str:
    """Reconcile one comment while the caller holds the stored PR's review lock."""
    body = owner_feedback_comment(decision, review_url=review_url)
    marker = body.splitlines()[0]
    path = f"/repos/{quote(decision.repository)}/issues/{decision.pull_request_number}/comments"
    matches: list[dict[str, object]] = []
    for page in range(1, 11):
        comments = api_request(path=f"{path}?per_page=100&page={page}", token=token)
        if not isinstance(comments, list):
            raise ValueError("Owner feedback comment lookup is unavailable.")
        for comment in comments:
            if not isinstance(comment, dict):
                raise ValueError("Owner feedback comment lookup is incomplete.")
            text = comment.get("body")
            if not isinstance(text, str) or not text.startswith(marker + "\n"):
                continue
            author = comment.get("user")
            if not isinstance(author, dict) or author.get("id") != actor_id:
                # A copied marker from another author is not our delivery receipt.
                continue
            matches.append(comment)
        if len(comments) < 100:
            break
    else:
        raise ValueError("Owner feedback comment lookup exceeds the supported size.")
    if len(matches) > 1:
        raise ValueError("More than one Owner feedback receipt was found.")
    comment = (
        matches[0]
        if matches
        else api_request(path=path, token=token, method="POST", body={"body": body})
    )
    comment_id = required_positive_int(
        comment.get("id") if isinstance(comment, dict) else None,
        "Owner feedback delivery was not confirmed.",
        error_type=ValueError,
    )
    if matches and matches[0].get("body") != body:
        # Repair our own unique receipt from the authoritative saved record. A
        # changed public origin or an edited comment must not strand a decision
        # after a successful POST whose response/receipt write was lost.
        updated = api_request(
            path=f"/repos/{quote(decision.repository)}/issues/comments/{comment_id}",
            token=token,
            method="PATCH",
            body={"body": body},
        )
        if not isinstance(updated, dict) or updated.get("id") != comment_id:
            raise ValueError("Owner feedback repair was not confirmed.")
    return (
        f"https://github.com/{decision.repository}/pull/{decision.pull_request_number}"
        f"#issuecomment-{comment_id}"
    )
