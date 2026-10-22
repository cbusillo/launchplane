"""Classify why a merge-train candidate's checks failed.

A candidate fails either because a queued change broke the combined tree or
because the runner infrastructure did not finish the work. The controller
retries the second kind and isolates the change behind the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from control_plane.github_required_checks import CheckRequirement

from control_plane.contracts.advisory_check_projection import is_launchplane_projected_check

# Conclusions GitHub gives a check run that never ran its work to the end.
INFRASTRUCTURE_CONCLUSIONS = frozenset({"cancelled", "stale", "startup_failure"})
# Conclusions that can carry a real failure; their annotations are read.
FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required"})
# GitHub's own annotations when a runner is lost, shut down, or never assigned.
RUNNER_LOSS_MESSAGES = (
    "lost communication with the server",
    "received a shutdown signal",
    "was not acquired by runner",
)
# A CI job names each failed test with an annotation titled this way.
FAILED_TEST_ANNOTATION_TITLE = "failed test"
MAX_ANNOTATED_CHECK_RUNS = 20
MAX_NAMED_FAILURES = 5
_SAFE_CHECK_NAME = re.compile(r"[\w .:/(),-]{1,100}")
_SAFE_TEST_ID = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")


@dataclass(frozen=True)
class CandidateCheckFailure:
    name: str
    conclusion: str
    url: str
    # GitHub annotated the run as lost, shut down, or never given a runner.
    runner_lost: bool = False
    failed_tests: tuple[str, ...] = ()
    source: Literal["check_run", "commit_status"] = "check_run"
    # Provider identity stays separate from the sanitized name rendered in feedback.
    identity_name: str = ""


@dataclass(frozen=True)
class CandidateCheckFailures:
    checks: tuple[CandidateCheckFailure, ...]
    # A check run is still queued or running, for example a re-run of a failed job.
    checks_pending: bool = False
    attribution_requirements: tuple[CheckRequirement, ...] = ()
    retry_runs: tuple[tuple[int, int], ...] = ()
    checks_disagree: bool = False

    @property
    def conclusive(self) -> bool:
        """Whether this evidence can justify a retry, a split or a block."""
        return bool(self.checks) and not self.checks_pending

    @property
    def infrastructure_only(self) -> bool:
        """Retry only when the runners failed and no check named a real failure.

        Jobs that depend on a lost job also fail, with a generic exit-code
        annotation, so a runner-loss annotation is enough. A bare cancellation
        is not: fail-fast also cancels the siblings of a real failure, so it
        counts only when no run failed outright. A named failed test or a
        failing commit status always rules infrastructure out.
        """
        if any(check.failed_tests or check.source == "commit_status" for check in self.checks):
            return False
        if any(check.runner_lost for check in self.checks):
            return True
        return any(
            check.conclusion in INFRASTRUCTURE_CONCLUSIONS for check in self.checks
        ) and not any(check.conclusion in FAILURE_CONCLUSIONS for check in self.checks)

    @property
    def check_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(check.name for check in self.checks if check.name))

    @property
    def check_identity_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(check.identity_name or check.name for check in self.checks))

    @property
    def failed_tests(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(test for check in self.checks for test in check.failed_tests))


def classify_candidate_check_failures(
    *,
    check_runs: list[dict[str, object]],
    commit_statuses: list[dict[str, object]],
    read_annotations: Callable[[int], list[dict[str, object]]],
) -> CandidateCheckFailures:
    """Name each failing check and decide whether runner infrastructure caused it.

    ``read_annotations`` is called for at most ``MAX_ANNOTATED_CHECK_RUNS``
    failed or cancelled check runs.
    """
    failures: list[CandidateCheckFailure] = []
    annotated = 0
    checks_pending = False
    for check_run in check_runs:
        name = _text(check_run.get("name"))
        if is_launchplane_projected_check(name):
            continue
        if _text(check_run.get("status")).strip().lower() != "completed":
            checks_pending = True
            continue
        conclusion = _text(check_run.get("conclusion")).strip().lower()
        if conclusion not in INFRASTRUCTURE_CONCLUSIONS | FAILURE_CONCLUSIONS:
            continue
        runner_lost = False
        failed_tests: list[str] = []
        check_run_id = _check_run_id(check_run.get("id"))
        if check_run_id is not None and annotated < MAX_ANNOTATED_CHECK_RUNS:
            annotated += 1
            for annotation in read_annotations(check_run_id):
                message = _text(annotation.get("message")).strip()
                if any(signature in message.lower() for signature in RUNNER_LOSS_MESSAGES):
                    runner_lost = True
                title = _text(annotation.get("title")).strip().casefold()
                if title == FAILED_TEST_ANNOTATION_TITLE and _SAFE_TEST_ID.fullmatch(message):
                    failed_tests.append(message)
        failures.append(
            CandidateCheckFailure(
                name=_safe_check_name(name),
                identity_name=name,
                conclusion=conclusion,
                url=_safe_url(check_run.get("html_url")),
                runner_lost=runner_lost,
                failed_tests=tuple(failed_tests),
            )
        )
    seen_contexts: set[str] = set()
    for status in commit_statuses:
        context = _text(status.get("context"))
        if is_launchplane_projected_check(context) or context.casefold() in seen_contexts:
            continue
        seen_contexts.add(context.casefold())
        state = _text(status.get("state")).strip().lower()
        if state in {"failure", "error"}:
            failures.append(
                CandidateCheckFailure(
                    name=_safe_check_name(context),
                    identity_name=context,
                    conclusion=state,
                    url="",
                    source="commit_status",
                )
            )
    return CandidateCheckFailures(checks=tuple(failures), checks_pending=checks_pending)


def _more(count: int) -> str:
    return f" and {count - MAX_NAMED_FAILURES} more" if count > MAX_NAMED_FAILURES else ""


def _safe_check_name(name: str) -> str:
    compact = " ".join(name.split())
    return compact if _SAFE_CHECK_NAME.fullmatch(compact) else "unnamed check"


def _check_run_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_url(value: object) -> str:
    url = _text(value).strip()
    return url if url.startswith("https://") and " " not in url else ""
