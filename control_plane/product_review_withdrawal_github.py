"""GitHub is the current source-control adapter for request withdrawal."""

from dataclasses import dataclass
from collections.abc import Callable
import re
from urllib.parse import quote

from control_plane.product_review_withdrawal import ReviewWithdrawalRefused
from control_plane.github_payload import required_positive_int


@dataclass
class GitHubReviewWithdrawalSource:
    repository: str
    pull_request: int
    token: str
    api_request: Callable[..., object]
    app_id: int = 0

    @property
    def path(self) -> str:
        return f"/repos/{quote(self.repository, safe='/')}/issues/{self.pull_request}"

    def change(self) -> tuple[str, frozenset[str]]:
        change = self.api_request(
            path=f"/repos/{quote(self.repository, safe='/')}/pulls/{self.pull_request}",
            token=self.token,
        )
        if not isinstance(change, dict) or change.get("state") != "open":
            raise ReviewWithdrawalRefused("The pull request is unavailable or closed.")
        head = change.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        labels = change.get("labels")
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[a-f0-9]{40}", sha)
            or not isinstance(labels, list)
            or any(
                not isinstance(label, dict) or not isinstance(label.get("name"), str)
                for label in labels
            )
        ):
            raise ReviewWithdrawalRefused("Current pull request evidence is incomplete.")
        return sha, frozenset(label["name"] for label in labels)

    def remove_label(self, label: str) -> None:
        # Never swallow provider errors or switch credentials.
        self.api_request(
            path=f"{self.path}/labels/{quote(label, safe='')}", token=self.token, method="DELETE"
        )

    def history(self, marker: str, body: str = "") -> str:
        if not marker:
            return ""
        actor_id = 0
        if not self.app_id:
            actor = self.api_request(path="/user", token=self.token)
            actor_id = required_positive_int(
                actor.get("id") if isinstance(actor, dict) else None,
                "Withdrawal publishing identity is unavailable.",
                error_type=ValueError,
            )
        matches: list[dict[str, object]] = []
        for page in range(1, 11):
            comments = self.api_request(
                path=f"{self.path}/comments?per_page=100&page={page}", token=self.token
            )
            if not isinstance(comments, list):
                raise ReviewWithdrawalRefused("Withdrawal history lookup is unavailable.")
            for comment in comments:
                if not isinstance(comment, dict):
                    raise ReviewWithdrawalRefused("Withdrawal history lookup is incomplete.")
                user = comment.get("user")
                app = comment.get("performed_via_github_app")
                matches_actor = (
                    (isinstance(app, dict) and app.get("id") == self.app_id)
                    if self.app_id
                    else (isinstance(user, dict) and user.get("id") == actor_id)
                )
                text = comment.get("body")
                if matches_actor and isinstance(text, str) and text.startswith(marker + "\n"):
                    matches.append(comment)
            if len(comments) < 100:
                break
        else:
            raise ReviewWithdrawalRefused("Withdrawal history lookup exceeded its bound.")
        if len(matches) > 1:
            raise ReviewWithdrawalRefused("Withdrawal history is ambiguous.")
        if not matches and not body:
            return ""
        comment = (
            matches[0]
            if matches
            else self.api_request(
                path=f"{self.path}/comments", token=self.token, method="POST", body={"body": body}
            )
        )
        if not isinstance(comment, dict) or (body and comment.get("body") != body):
            raise ReviewWithdrawalRefused("Withdrawal history was not confirmed.")
        comment_id = required_positive_int(
            comment.get("id"), "Withdrawal history was not confirmed.", error_type=ValueError
        )
        return f"https://github.com/{self.repository}/pull/{self.pull_request}#issuecomment-{comment_id}"
