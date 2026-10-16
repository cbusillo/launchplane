"""Provider-neutral, current-head engineering handoffs for train admission."""

from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy


class MergeTrainReadyHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "missing", "waiting", "stale", "unreadable", "edited"]
    head_sha: str = ""
    actor_id: int | None = None
    actor_role: str = "unknown"
    comment_id: int | None = None


def handoff_kind(body: str) -> Literal["ready", "waiting"] | None:
    first = body.splitlines()[0].strip().removesuffix(".") if body.splitlines() else ""
    if first.startswith("**") and first.endswith("**"):
        first = first[2:-2].removesuffix(".")
    if first == "Ready for the merge train":
        return "ready"
    if first.startswith("Engineering ready; waiting for"):
        return "waiting"
    return None


def handoff_head(body: str) -> str:
    # Match the checked handoff producer's full SHA line, including its optional
    # presentation markup. Multiple declarations and quoted examples prove nothing.
    lines: list[str] = []
    fence = ""
    for line in re.sub(r"<!--[\s\S]*?-->", "", body).splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip()
            ):
                fence = ""
            continue
        if not fence:
            lines.append(line)
    heads = re.findall(
        r"(?im)^Head(?: SHA)?:[ \t]*(?P<marker>\*\*|`)?"
        r"(?P<sha>[0-9a-f]{40})(?(marker)(?P=marker))(?:[.\s]|$)",
        "\n".join(lines),
    )
    return heads[0][1] if len(heads) == 1 else ""


def handoff_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Ready handoff timestamp requires a timezone.")
    return stamp


def handoff_actor_allowed(
    policy: MergeTrainRepositoryPolicy, *, actor_id: int | None, actor_role: str
) -> bool:
    return actor_id is not None and (
        actor_id in policy.enqueue.trusted_automation_github_user_ids
        or actor_role in policy.enqueue.allowed_actor_roles
    )


def ready_handoff_refusal(
    policy: MergeTrainRepositoryPolicy, handoff: MergeTrainReadyHandoff | None, head_sha: str
) -> str:
    if handoff is None or handoff.status == "missing":
        return "missing trusted current-head Ready handoff"
    if handoff.status == "edited":
        return "trusted comment was edited; post a fresh checked gh-pr.py ready handoff"
    if handoff.status == "unreadable":
        return "Ready handoff evidence is unreadable"
    if handoff.status == "waiting":
        return "latest trusted engineering handoff is waiting"
    if handoff.status == "stale" or handoff.head_sha != head_sha:
        return "Ready handoff does not name exactly the current head; post a fresh checked gh-pr.py ready handoff"
    if not handoff_actor_allowed(policy, actor_id=handoff.actor_id, actor_role=handoff.actor_role):
        return "Ready handoff author is not allowed to enqueue"
    return ""
