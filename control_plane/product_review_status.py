"""Project saved Client decisions through the Delivery and Advisory GitHub Apps.

Delivery is best-effort: saved decisions and comments do not depend on GitHub
accepting the owner-review check run.
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import hashlib
from pathlib import Path
from typing import Final, Literal
from urllib.parse import quote, urlencode, urlsplit

from control_plane.advisory_check_projection import write_advisory_check_projection
from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckProjection,
    OWNER_ACCEPTANCE_CHECK_NAME,
    OWNER_REVIEW_CHECK_NAME,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOwnerProfile,
)
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.github_app_identity import (
    GitHubAppInstallationToken,
    mint_repository_installation_token,
    resolve_advisory_github_app_identity,
    revoke_installation_token,
)
from control_plane.product_review import ProductReviewStore
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id
from control_plane.product_review_carry import carry_owner_acceptance, record_decision_base
from control_plane.product_review_feedback import publish_owner_feedback
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
)

OWNER_REVIEW_STATUS_CONTEXT: Final = OWNER_REVIEW_CHECK_NAME
NO_OWNER_DESCRIPTION: Final = "No Client set for this product"
RETIRED_MANAGER_STATUS_DESCRIPTION: Final = "Retired. Client review is recorded in Launchplane."
RETIRED_OWNER_ACCEPTANCE_TITLE: Final = "Retired"
RETIRED_OWNER_ACCEPTANCE_SUMMARY: Final = (
    f"Client review for this pull request is shown by the `{OWNER_REVIEW_STATUS_CONTEXT}` check."
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
    base_branch: str = ""


def owner_review_status(
    *,
    owner: ProductOwnerProfile,
    head_sha: str,
    decisions: tuple[ProductReviewDecisionRecord, ...],
    base_branch: str = "",
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
    if (
        decision is not None
        and decision.carried_from is not None
        and decision.base_branch != base_branch.strip()
    ):
        # Carried on another base: retargeting changed the change, so it no longer applies.
        decision = None
    if decision is None:
        return OwnerReviewStatus(
            state="pending",
            description=f"Waiting for @{owner.github_login} to review the preview",
        )
    if decision.feedback_requested and not decision.feedback_url:
        return OwnerReviewStatus(state="pending", description="Client feedback delivery is pending")
    if decision.decision == "accepted":
        description = f"Accepted by @{decision.owner_github_login}"
        if decision.carried_from is not None:
            description += (
                f" (carried from {decision.carried_from.head_sha[:7]} after a base-only refresh)"
            )
        return OwnerReviewStatus(state="success", description=description)
    return OwnerReviewStatus(
        state="failure", description=f"Changes requested by @{decision.owner_github_login}"
    )


@dataclass(frozen=True, slots=True)
class OwnerReviewStatusPublisher:
    """Project Client review through independently scoped Delivery and Advisory Apps."""

    control_plane_root: Path
    public_origin: str | None = None
    github_token: GitHubTokenResolver = resolve_launchplane_github_token
    api_request: GitHubApiRequest = github_api_request
    github_app_token: GitHubAppTokenMinter | None = None
    github_feedback_app_id: Callable[..., int] = resolve_delivery_github_app_id

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
                repository=repository,
                purpose="pull_request_feedback",
            ).strip()
            if not token or "/" not in repository:
                _LOGGER.info(
                    "Client review status skipped: no source-control credential.",
                    extra={"repository": repository, "pull_request_number": pull_request_number},
                )
                return None
            facts = self._pull_request_facts(
                repository=repository, pull_request_number=pull_request_number, token=token
            )
        except Exception:
            _LOGGER.warning(
                "Client review status could not read the pull request.",
                exc_info=True,
                extra={"repository": repository, "pull_request_number": pull_request_number},
            )
            return None
        written: OwnerReviewStatus | None = None
        try:
            with store.product_review_lock(
                repository=repository, pull_request_number=pull_request_number, purpose="feedback"
            ):
                try:
                    self._publish_feedback(
                        store=store,
                        profile=profile,
                        pull_request_number=pull_request_number,
                        token=token,
                    )
                except Exception:
                    _LOGGER.warning(
                        "Client decision is saved but feedback delivery is pending.",
                        exc_info=True,
                        extra={
                            "repository": repository,
                            "pull_request_number": pull_request_number,
                        },
                    )
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
                "Client review status could not be written.",
                exc_info=True,
                extra={"repository": repository, "pull_request_number": pull_request_number},
            )
        if retire_leftovers:
            for retire in (self._retire_owner_acceptance_check,):
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

    def _publish_feedback(
        self,
        *,
        store: ProductReviewStore,
        profile: LaunchplaneProductProfileRecord,
        pull_request_number: int,
        token: str,
    ) -> None:
        # The caller serializes delivery and its resulting status. Decision saves
        # use a separate lock so slow provider I/O cannot prevent persistence.
        pending = tuple(
            decision
            for decision in store.list_product_review_decision_records(
                repository=profile.repository, pull_request_number=pull_request_number
            )
            if decision.feedback_requested and not decision.feedback_url
        )
        if not pending:
            return
        if not self.public_origin:
            raise ValueError("Client feedback needs the public review origin.")
        app_id = self.github_feedback_app_id(control_plane_root=self.control_plane_root)
        for decision in reversed(pending):
            try:
                feedback_url = publish_owner_feedback(
                    decision=decision,
                    review_url=owner_review_reference_url(
                        public_origin=self.public_origin,
                        repository=profile.repository,
                        pull_request_number=pull_request_number,
                        decision_id=decision.record_id,
                    ),
                    token=token,
                    app_id=app_id,
                    api_request=self.api_request,
                )
                store.write_product_review_decision_record(
                    decision.model_copy(update={"feedback_url": feedback_url})
                )
            except Exception:
                # Keep this receipt pending, but do not strand later feedback.
                _LOGGER.warning(
                    "Saved Client decision feedback delivery is pending.",
                    exc_info=True,
                    extra={"repository": profile.repository, "record_id": decision.record_id},
                )

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
        base_ref = base.get("ref") if isinstance(base, dict) else None
        base_repository = base.get("repo") if isinstance(base, dict) else None
        raw_repository_id = base_repository.get("id") if isinstance(base_repository, dict) else None
        repository_id = (
            str(raw_repository_id)
            if isinstance(raw_repository_id, int) and not isinstance(raw_repository_id, bool)
            else ""
        )
        return _PullRequestFacts(
            head_sha=head_sha.strip().lower(),
            labels=labels,
            repository_id=repository_id,
            base_branch=base_ref.strip() if isinstance(base_ref, str) else "",
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
        try:
            record_decision_base(
                store=store,
                profile=profile,
                pull_request_number=pull_request_number,
                head_sha=facts.head_sha,
                base_branch=facts.base_branch,
            )
            carry_owner_acceptance(
                store=store,
                profile=profile,
                pull_request_number=pull_request_number,
                head_sha=facts.head_sha,
                base_branch=facts.base_branch,
                read=lambda path: self.api_request(path=path, token=token),
            )
        except Exception:
            # Not carried: the head waits for the Client, as it would without a carry.
            _LOGGER.warning(
                "Client acceptance could not be checked for a carry.",
                exc_info=True,
                extra={
                    "repository": profile.repository,
                    "pull_request_number": pull_request_number,
                },
            )
        status = owner_review_status(
            owner=profile.owner,
            head_sha=facts.head_sha,
            decisions=store.list_product_review_decision_records(
                repository=profile.repository,
                pull_request_number=pull_request_number,
            ),
            base_branch=facts.base_branch,
        )
        if not facts.repository_id or not self.public_origin:
            raise ValueError("Client review check requires the Advisory App and public origin.")
        projection = AdvisoryCheckProjection(
            name=OWNER_REVIEW_CHECK_NAME,
            repository=profile.repository,
            repository_id=facts.repository_id,
            head_sha=facts.head_sha,
            external_id=hashlib.sha256(
                f"{profile.repository}:{pull_request_number}:{facts.head_sha}".encode()
            ).hexdigest(),
            details_url=owner_review_reference_url(
                public_origin=self.public_origin,
                repository=profile.repository,
                pull_request_number=pull_request_number,
            ),
            title=status.description,
            summary="Client review is recorded in Launchplane; this check grants no merge or deployment authority.",
            check_status="in_progress" if status.state == "pending" else "completed",
            conclusion=None if status.state == "pending" else status.state,
        )
        installation_token = self._advisory_token(profile.repository, facts.repository_id)
        try:
            write_advisory_check_projection(
                projection=projection,
                installation_token=installation_token,
                api_request=self.api_request,
            )
        finally:
            revoke_installation_token(
                installation_token=installation_token,
                api_request=self.api_request,
            )
        return status

    def _advisory_token(self, repository: str, repository_id: str) -> GitHubAppInstallationToken:
        if self.github_app_token is not None:
            return self.github_app_token(repository, repository_id)
        return mint_repository_installation_token(
            identity=resolve_advisory_github_app_identity(
                control_plane_root=self.control_plane_root
            ),
            repository=repository,
            repository_id=repository_id,
            api_request=self.api_request,
        )

    def _retire_owner_acceptance_check(
        self, *, repository: str, facts: _PullRequestFacts, token: str
    ) -> None:
        del token  # Only the app that created a check run may update it.
        if not facts.repository_id:
            return
        installation_token = self._advisory_token(repository, facts.repository_id)
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


def owner_review_reference_url(
    *,
    public_origin: str,
    repository: str,
    pull_request_number: int,
    decision_id: str = "",
) -> str:
    origin = public_origin.strip()
    try:
        parsed_origin = urlsplit(origin)
    except ValueError as error:
        raise ValueError("Client review requires a valid browser public origin.") from error
    if (
        parsed_origin.scheme not in {"http", "https"}
        or not parsed_origin.netloc
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        raise ValueError("Client review requires a valid browser public origin.")
    try:
        if parsed_origin.port is not None and not 1 <= parsed_origin.port <= 65535:
            raise ValueError
    except ValueError as error:
        raise ValueError("Client review requires a valid browser public origin.") from error
    if any(character.isspace() or ord(character) < 32 for character in origin):
        raise ValueError("Client review requires a valid browser public origin.")
    if repository.count("/") != 1 or any(
        not part or part != part.strip() for part in repository.split("/", 1)
    ):
        raise ValueError("Client review requires a valid repository target.")
    if pull_request_number < 1:
        raise ValueError("Client review requires a positive pull request number.")
    url = (
        f"{origin.rstrip('/')}/ui/owner-review"
        f"?repository={quote(repository, safe='')}&pull_request={pull_request_number}"
    )
    return f"{url}&decision_id={quote(decision_id, safe='')}" if decision_id else url
