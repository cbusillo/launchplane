"""The one Owner-review signal Launchplane shows on a pull request.

A marked pull request gets the commit status ``launchplane/owner-review`` on its
current head. Delivery is best-effort: a recorded decision and a delivered preview
comment never depend on the source-control provider accepting the status.
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Final, Literal
from urllib.parse import quote, urlencode

from control_plane.contracts.advisory_check_projection import OWNER_ACCEPTANCE_CHECK_NAME
from control_plane.contracts.manager_preview_approval_projection import (
    MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOwnerProfile,
)
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.github_app_identity import (
    GitHubAppInstallationToken,
    revoke_installation_token,
)
from control_plane.owner_acceptance_projection import owner_review_reference_url
from control_plane.product_review import ProductReviewStore
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
)

OWNER_REVIEW_STATUS_CONTEXT: Final = "launchplane/owner-review"
NO_OWNER_DESCRIPTION: Final = "No Owner set for this product"
RETIRED_MANAGER_STATUS_DESCRIPTION: Final = "Retired. Owner review is recorded in Launchplane."
RETIRED_OWNER_ACCEPTANCE_TITLE: Final = "Retired"
RETIRED_OWNER_ACCEPTANCE_SUMMARY: Final = (
    f"Owner review for this pull request is shown by the `{OWNER_REVIEW_STATUS_CONTEXT}` status."
)

OwnerReviewStatusState = Literal["pending", "success", "failure"]
GitHubApiRequest = Callable[..., object]
GitHubTokenResolver = Callable[..., str]
GitHubAppTokenMinter = Callable[[str, str], GitHubAppInstallationToken]

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnerReviewStatus:
    state: OwnerReviewStatusState
    description: str


@dataclass(frozen=True, slots=True)
class _PullRequestFacts:
    head_sha: str
    labels: frozenset[str]
    repository_id: str


def owner_review_status(
    *,
    owner: ProductOwnerProfile,
    head_sha: str,
    decisions: tuple[ProductReviewDecisionRecord, ...],
) -> OwnerReviewStatus:
    """Return the status for a marked pull request; `decisions` are newest first."""

    if not owner.is_set:
        return OwnerReviewStatus(state="pending", description=NO_OWNER_DESCRIPTION)
    current_head = head_sha.strip().lower()
    # The Owner reviews what is actually previewed, so a decision for another head
    # says nothing about this one.
    decision = next(
        (record for record in decisions if record.head_sha.strip().lower() == current_head),
        None,
    )
    if decision is None:
        return OwnerReviewStatus(
            state="pending",
            description=f"Waiting for @{owner.github_login} to review the preview",
        )
    if decision.decision == "accepted":
        return OwnerReviewStatus(
            state="success", description=f"Accepted by @{decision.owner_github_login}"
        )
    return OwnerReviewStatus(
        state="failure", description=f"Changes requested by @{decision.owner_github_login}"
    )


@dataclass(frozen=True, slots=True)
class OwnerReviewStatusPublisher:
    """Writes the Owner-review status with the repository's preview feedback credential."""

    control_plane_root: Path
    public_origin: str | None = None
    github_token: GitHubTokenResolver = resolve_launchplane_github_token
    api_request: GitHubApiRequest = github_api_request
    github_app_token: GitHubAppTokenMinter | None = None

    def publish(
        self,
        *,
        store: ProductReviewStore,
        profile: LaunchplaneProductProfileRecord,
        pull_request_number: int,
        context: str = "",
        retire_leftovers: bool = False,
    ) -> OwnerReviewStatus | None:
        """Best-effort: never raises. Returns the status written, if any."""

        repository = profile.repository.strip()
        try:
            token = self.github_token(
                control_plane_root=self.control_plane_root,
                context_name=context.strip() or profile.preview.context,
            ).strip()
            if not token or "/" not in repository:
                _LOGGER.info(
                    "Owner review status skipped: no source-control credential.",
                    extra={"repository": repository, "pull_request_number": pull_request_number},
                )
                return None
            facts = self._pull_request_facts(
                repository=repository, pull_request_number=pull_request_number, token=token
            )
        except Exception:
            _LOGGER.warning(
                "Owner review status could not read the pull request.",
                exc_info=True,
                extra={"repository": repository, "pull_request_number": pull_request_number},
            )
            return None
        written: OwnerReviewStatus | None = None
        try:
            if profile.owner.review_label.strip().casefold() in facts.labels:
                written = self._write_status(
                    store=store,
                    profile=profile,
                    pull_request_number=pull_request_number,
                    facts=facts,
                    token=token,
                )
        except Exception:
            _LOGGER.warning(
                "Owner review status could not be written.",
                exc_info=True,
                extra={"repository": repository, "pull_request_number": pull_request_number},
            )
        if retire_leftovers:
            for retire in (self._retire_manager_status, self._retire_owner_acceptance_check):
                try:
                    retire(repository=repository, facts=facts, token=token)
                except Exception:
                    _LOGGER.warning(
                        "A retired pull request signal could not be updated.",
                        exc_info=True,
                        extra={
                            "repository": repository,
                            "pull_request_number": pull_request_number,
                        },
                    )
        return written

    def _pull_request_facts(
        self, *, repository: str, pull_request_number: int, token: str
    ) -> _PullRequestFacts:
        payload = self.api_request(
            path=f"/repos/{_repository_path(repository)}/pulls/{pull_request_number}",
            token=token,
        )
        if not isinstance(payload, dict):
            raise ValueError("Pull request response must be an object.")
        head = payload.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise ValueError("Pull request response is missing its head revision.")
        raw_labels = payload.get("labels")
        labels = frozenset(
            item["name"].strip().casefold()
            for item in (raw_labels if isinstance(raw_labels, list) else ())
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        base = payload.get("base")
        base_repository = base.get("repo") if isinstance(base, dict) else None
        raw_repository_id = base_repository.get("id") if isinstance(base_repository, dict) else None
        repository_id = (
            str(raw_repository_id)
            if isinstance(raw_repository_id, int) and not isinstance(raw_repository_id, bool)
            else ""
        )
        return _PullRequestFacts(
            head_sha=head_sha.strip().lower(), labels=labels, repository_id=repository_id
        )

    def _write_status(
        self,
        *,
        store: ProductReviewStore,
        profile: LaunchplaneProductProfileRecord,
        pull_request_number: int,
        facts: _PullRequestFacts,
        token: str,
    ) -> OwnerReviewStatus:
        status = owner_review_status(
            owner=profile.owner,
            head_sha=facts.head_sha,
            decisions=store.list_product_review_decision_records(
                repository=profile.repository,
                pull_request_number=pull_request_number,
            ),
        )
        body: dict[str, object] = {
            "state": status.state,
            "description": status.description,
            "context": OWNER_REVIEW_STATUS_CONTEXT,
        }
        if self.public_origin:
            body["target_url"] = owner_review_reference_url(
                public_origin=self.public_origin,
                repository=profile.repository,
                pull_request_number=pull_request_number,
            )
        self._post_status(
            repository=profile.repository, head_sha=facts.head_sha, token=token, body=body
        )
        return status

    def _post_status(
        self, *, repository: str, head_sha: str, token: str, body: dict[str, object]
    ) -> None:
        self.api_request(
            path=f"/repos/{_repository_path(repository)}/statuses/{quote(head_sha, safe='')}",
            token=token,
            method="POST",
            body=body,
        )

    def _retire_manager_status(
        self, *, repository: str, facts: _PullRequestFacts, token: str
    ) -> None:
        payload = self.api_request(
            path=(
                f"/repos/{_repository_path(repository)}/commits/"
                f"{quote(facts.head_sha, safe='')}/statuses?per_page=100"
            ),
            token=token,
        )
        if not isinstance(payload, list):
            raise ValueError("Commit statuses response must be a list.")
        # The provider lists statuses newest first; the first match is the visible one.
        current = next(
            (
                item
                for item in payload
                if isinstance(item, dict)
                and item.get("context") == MANAGER_PREVIEW_APPROVAL_CHECK_NAME
            ),
            None,
        )
        if current is None or current.get("description") == RETIRED_MANAGER_STATUS_DESCRIPTION:
            return
        self._post_status(
            repository=repository,
            head_sha=facts.head_sha,
            token=token,
            body={
                "state": "success",
                "description": RETIRED_MANAGER_STATUS_DESCRIPTION,
                "context": MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
            },
        )

    def _retire_owner_acceptance_check(
        self, *, repository: str, facts: _PullRequestFacts, token: str
    ) -> None:
        del token  # Only the app that created a check run may update it.
        if self.github_app_token is None or not facts.repository_id:
            return
        installation_token = self.github_app_token(repository, facts.repository_id)
        try:
            query = urlencode(
                {
                    "check_name": OWNER_ACCEPTANCE_CHECK_NAME,
                    "app_id": str(installation_token.app_id),
                    "filter": "latest",
                    "per_page": "100",
                }
            )
            payload = self.api_request(
                path=(
                    f"/repos/{_repository_path(repository)}/commits/"
                    f"{quote(facts.head_sha, safe='')}/check-runs?{query}"
                ),
                token=installation_token.token,
            )
            check_runs = payload.get("check_runs") if isinstance(payload, dict) else None
            if not isinstance(check_runs, list):
                raise ValueError("Check runs response must include check_runs.")
            for check_run in check_runs:
                if not isinstance(check_run, dict):
                    continue
                app = check_run.get("app")
                check_run_id = check_run.get("id")
                if (
                    check_run.get("name") != OWNER_ACCEPTANCE_CHECK_NAME
                    or not isinstance(app, dict)
                    or app.get("id") != installation_token.app_id
                    or not isinstance(check_run_id, int)
                    or check_run.get("conclusion") == "neutral"
                ):
                    continue
                self.api_request(
                    path=f"/repos/{_repository_path(repository)}/check-runs/{check_run_id}",
                    token=installation_token.token,
                    method="PATCH",
                    body={
                        "name": OWNER_ACCEPTANCE_CHECK_NAME,
                        "status": "completed",
                        "conclusion": "neutral",
                        "output": {
                            "title": RETIRED_OWNER_ACCEPTANCE_TITLE,
                            "summary": RETIRED_OWNER_ACCEPTANCE_SUMMARY,
                        },
                    },
                )
        finally:
            revoke_installation_token(
                installation_token=installation_token, api_request=self.api_request
            )


def _repository_path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
