import json
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.advisory_check_projection import is_launchplane_projected_check
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.merge_train_batch import MergeTrainBatchEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlan
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapseBranchClient
from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.github_payload import json_object
from control_plane.github_payload import required_positive_int
from control_plane.github_payload import required_string_text
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import MergeTrainMergeableState
from control_plane.merge_train import MergeTrainPullRequestSnapshot
from control_plane.merge_train import MergeTrainPullRequestState


class MergeTrainGitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MergeTrainGitHubStaleHeadError(MergeTrainGitHubError):
    """Raised when GitHub refuses a guarded merge because the head SHA changed."""


class MergeTrainGitHubTransport(Protocol):
    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object: ...


class UrllibMergeTrainGitHubTransport:
    def __init__(self, *, token: str, api_base_url: str = "https://api.github.com") -> None:
        self.token = _required_value(token, "GitHub token is required.")
        self.api_base_url = _required_value(
            api_base_url, "GitHub API base URL is required."
        ).rstrip("/")

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        request_body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            request_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            url=f"{self.api_base_url}{path}",
            method=method,
            headers=headers,
            data=request_body,
        )
        try:
            with urlopen(request, timeout=15) as response:
                response_text = response.read().decode("utf-8")
                return json.loads(response_text) if response_text.strip() else None
        except HTTPError as error:
            raise _github_http_error(path=path, status_code=error.code, error=error) from error
        except (URLError, OSError) as error:
            raise MergeTrainGitHubError(f"GitHub API request failed for {path}: {error}") from error
        except json.JSONDecodeError as error:
            raise MergeTrainGitHubError(
                f"GitHub API response for {path} was not valid JSON."
            ) from error


class GitHubMergeTrainClient(MergeTrainStackCollapseBranchClient):
    def __init__(self, *, transport: MergeTrainGitHubTransport) -> None:
        self.transport = transport

    def build_batch_candidate(
        self,
        *,
        candidate: MergeTrainBatchCandidate,
        checkpoint: (
            Callable[[MergeTrainBatchCandidate, MergeTrainBatchEntry | None, str], None] | None
        ) = None,
    ) -> MergeTrainBatchCandidate:
        repository_path = _repository_path(candidate.repository)
        candidate_branch = _branch_name_from_ref(candidate.candidate_ref)
        if checkpoint is not None:
            checkpoint(candidate, None, "reset_candidate_ref")
        self._create_or_reset_reference(
            repository_path=repository_path,
            reference=candidate.candidate_ref,
            sha=candidate.base_sha,
        )
        if checkpoint is not None:
            checkpoint(candidate, None, "candidate_ref_ready")
        candidate_sha = candidate.base_sha
        for entry_index, entry in enumerate(candidate.entries, start=1):
            if checkpoint is not None:
                checkpoint(candidate, entry, "merge_candidate_entry")
            payload = self.transport.request(
                method="POST",
                path=f"/repos/{repository_path}/merges",
                body={
                    "base": candidate_branch,
                    "head": entry.head_sha,
                    "commit_message": (
                        f"Launchplane merge train {candidate.batch_id}: "
                        f"merge PR #{entry.pull_request_number}"
                    ),
                },
            )
            if payload is None:
                if checkpoint is not None:
                    checkpoint(
                        candidate.model_copy(update={"candidate_sha": candidate_sha}),
                        entry,
                        f"candidate_entry_merged:{entry_index}",
                    )
                continue
            if not isinstance(payload, dict):
                raise MergeTrainGitHubError("GitHub merge response must be a JSON object.")
            candidate_sha = _required_text(
                payload.get("sha"), "GitHub merge response requires sha."
            )
            if checkpoint is not None:
                checkpoint(
                    candidate.model_copy(update={"candidate_sha": candidate_sha}),
                    entry,
                    f"candidate_entry_merged:{entry_index}",
                )
        return candidate.model_copy(
            update={"candidate_sha": candidate_sha, "status": "ready_for_checks"}
        )

    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        repository_path = _repository_path(candidate.repository)
        candidate_sha = _required_value(
            candidate.candidate_sha,
            "Merge train batch candidate SHA is required before observing checks.",
        )
        check_status = _required_checks_status(
            transport=self.transport,
            repository_path=repository_path,
            encoded_head_sha=quote(candidate_sha, safe=""),
        )
        candidate_status = "ready_for_checks"
        if check_status == "pass":
            candidate_status = "passed"
        elif check_status == "fail":
            candidate_status = "failed"
        return candidate.model_copy(
            update={"required_checks_status": check_status, "status": candidate_status}
        )

    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        checkpoint: (
            Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str], None] | None
        ) = None,
    ) -> MergeTrainBatchLandingPlan:
        repository_path = _repository_path(landing_plan.repository)
        expected_base_sha = landing_plan.entries[0].expected_base_sha
        merged_entries = []
        for entry_index, entry in enumerate(landing_plan.entries):
            current_base_sha = _base_branch_sha(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=landing_plan.base_branch,
            )
            if entry.status == "merged":
                merge_commit_sha = _required_value(
                    entry.merge_commit_sha,
                    "Merged batch landing entry requires merge_commit_sha.",
                )
                if current_base_sha not in {expected_base_sha, merge_commit_sha}:
                    already_merged_entry = self._already_merged_landing_entry(
                        repository_path=repository_path,
                        entry=entry,
                        expected_base_ref=landing_plan.base_branch,
                    )
                    if already_merged_entry is None:
                        raise MergeTrainGitHubStaleHeadError(
                            "Base branch moved outside the batch landing plan.", status_code=409
                        )
                    if already_merged_entry.merge_commit_sha != merge_commit_sha:
                        raise MergeTrainGitHubStaleHeadError(
                            "Pull request merge commit does not match the batch landing plan.",
                            status_code=409,
                        )
                    if not self.branch_contains_commit(
                        repository=landing_plan.repository,
                        branch_ref=landing_plan.base_branch,
                        commit_sha=merge_commit_sha,
                    ):
                        raise MergeTrainGitHubStaleHeadError(
                            "Merged pull request commit is not contained by the target branch.",
                            status_code=409,
                        )
                merged_entries.append(entry)
                expected_base_sha = merge_commit_sha
                continue
            if current_base_sha != expected_base_sha:
                already_merged_entry = self._already_merged_landing_entry(
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=landing_plan.base_branch,
                )
                if already_merged_entry is None:
                    raise MergeTrainGitHubStaleHeadError(
                        "Base branch moved outside the batch landing plan.", status_code=409
                    )
                if not self.branch_contains_commit(
                    repository=landing_plan.repository,
                    branch_ref=landing_plan.base_branch,
                    commit_sha=already_merged_entry.merge_commit_sha,
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "Merged pull request commit is not contained by the target branch.",
                        status_code=409,
                    )
                merged_entries.append(already_merged_entry)
                expected_base_sha = already_merged_entry.merge_commit_sha
                if checkpoint is not None:
                    checkpoint(
                        landing_plan.model_copy(
                            update={
                                "entries": tuple(merged_entries)
                                + landing_plan.entries[entry_index + 1 :]
                            }
                        ),
                        already_merged_entry,
                        "entry_merged",
                    )
                continue
            self._validate_open_landing_pull_request(
                repository_path=repository_path,
                entry=entry,
                expected_base_ref=landing_plan.base_branch,
                expected_base_sha=expected_base_sha,
            )
            if checkpoint is not None:
                checkpoint(
                    landing_plan.model_copy(
                        update={
                            "entries": tuple(merged_entries) + landing_plan.entries[entry_index:]
                        }
                    ),
                    entry,
                    "merge_entry",
                )
            merge_commit_sha = self.merge_pull_request(
                repository=landing_plan.repository,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.expected_head_sha,
                merge_method=entry.merge_method,
            )
            merged_entry = entry.model_copy(
                update={"status": "merged", "merge_commit_sha": merge_commit_sha}
            )
            merged_entries.append(merged_entry)
            expected_base_sha = merge_commit_sha
            if checkpoint is not None:
                checkpoint(
                    landing_plan.model_copy(
                        update={
                            "entries": tuple(merged_entries)
                            + landing_plan.entries[entry_index + 1 :]
                        }
                    ),
                    merged_entry,
                    "entry_merged",
                )
        current_base_sha = _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=landing_plan.base_branch,
        )
        if current_base_sha != expected_base_sha:
            if not self.branch_contains_commit(
                repository=landing_plan.repository,
                branch_ref=landing_plan.base_branch,
                commit_sha=expected_base_sha,
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "Base branch moved outside the batch landing plan.", status_code=409
                )
        return landing_plan.model_copy(update={"entries": tuple(merged_entries)})

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        repository_path = _repository_path(landing_plan.repository)
        return self._delete_reference_if_present(
            repository_path=repository_path,
            reference=landing_plan.candidate_ref,
        )

    def candidate_ref_exists(self, *, repository: str, reference: str) -> bool:
        repository_path = _repository_path(repository)
        try:
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/git/ref/{_reference_path(reference)}",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    def pull_request_has_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> bool:
        pull_request = self._pull_request_detail(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        labels = pull_request.get("labels")
        if not isinstance(labels, list):
            return False
        normalized_label = _required_value(label, "GitHub label is required.")
        for label_payload in labels:
            if not isinstance(label_payload, dict):
                continue
            if str(label_payload.get("name") or "").strip() == normalized_label:
                return True
        return False

    def pull_request_is_closed(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> bool:
        pull_request = self._pull_request_detail(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR moved outside the stored collapse plan.",
                status_code=409,
            )
        return _pull_request_state(str(pull_request.get("state") or "")) == "closed"

    def find_pull_request_comment_url(
        self, *, repository: str, pull_request_number: int, body_contains: str
    ) -> str:
        repository_path = _repository_path(repository)
        payload = self.transport.request(
            method="GET",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/comments",
        )
        if not isinstance(payload, list):
            raise MergeTrainGitHubError("GitHub issue comments response must be a JSON list.")
        needle = _required_value(body_contains, "GitHub comment match text is required.")
        for item in payload:
            if not isinstance(item, dict):
                continue
            body = str(item.get("body") or "")
            if needle in body:
                return str(item.get("html_url") or "").strip()
        return ""

    def pull_request_is_merged(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> str:
        pull_request = self._pull_request_detail(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        if pull_request.get("merged") is not True:
            return ""
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request was merged with a different head SHA than the batch landing plan.",
                status_code=409,
            )
        return _required_text(
            pull_request.get("merge_commit_sha"),
            "Merged GitHub pull request requires merge_commit_sha.",
        )

    def branch_contains_commit(self, *, repository: str, branch_ref: str, commit_sha: str) -> bool:
        repository_path = _repository_path(repository)
        branch_name = quote(_required_value(branch_ref, "GitHub branch ref is required."), safe="")
        normalized_commit_sha = quote(
            _required_value(commit_sha, "GitHub commit SHA is required."),
            safe="",
        )
        payload = _json_object(
            self.transport.request(
                method="GET",
                path=(f"/repos/{repository_path}/compare/{normalized_commit_sha}...{branch_name}"),
            ),
            "GitHub compare response",
        )
        return str(payload.get("status") or "").strip() in {"ahead", "identical"}

    def branch_head_sha(self, *, repository: str, branch_ref: str) -> str:
        repository_path = _repository_path(repository)
        return _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=_required_value(branch_ref, "GitHub branch ref is required."),
        )

    def _pull_request_detail(
        self, *, repository: str, pull_request_number: int
    ) -> dict[str, object]:
        repository_path = _repository_path(repository)
        return _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{pull_request_number}",
            ),
            "GitHub pull request detail response",
        )

    def _already_merged_landing_entry(
        self,
        *,
        repository_path: str,
        entry: MergeTrainBatchLandingEntry,
        expected_base_ref: str,
    ) -> MergeTrainBatchLandingEntry | None:
        pull_request = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{entry.pull_request_number}",
            ),
            "GitHub pull request detail response",
        )
        if _pull_request_state(str(pull_request.get("state") or "")) != "closed":
            return None
        if pull_request.get("merged") is not True:
            return None
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        base_ref = _required_text(base.get("ref"), "GitHub pull request base requires ref.")
        if base_ref != _required_value(
            expected_base_ref, "Expected pull request base ref is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request was merged into a different base branch than the batch landing plan.",
                status_code=409,
            )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != entry.expected_head_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Pull request was merged with a different head SHA than the batch landing plan.",
                status_code=409,
            )
        merge_commit_sha = _required_text(
            pull_request.get("merge_commit_sha"),
            "Merged GitHub pull request requires merge_commit_sha.",
        )
        return entry.model_copy(update={"status": "merged", "merge_commit_sha": merge_commit_sha})

    def _validate_open_landing_pull_request(
        self,
        *,
        repository_path: str,
        entry: MergeTrainBatchLandingEntry,
        expected_base_ref: str,
        expected_base_sha: str,
    ) -> None:
        pull_request = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{entry.pull_request_number}",
            ),
            "GitHub pull request detail response",
        )
        if _pull_request_state(str(pull_request.get("state") or "")) != "open":
            raise MergeTrainGitHubStaleHeadError(
                "Pull request is no longer open for batch landing.", status_code=409
            )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != entry.expected_head_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Pull request head moved outside the batch landing plan.", status_code=409
            )
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        base_ref = _required_text(base.get("ref"), "GitHub pull request base requires ref.")
        base_sha = _required_text(base.get("sha"), "GitHub pull request base requires sha.")
        if base_ref != _required_value(
            expected_base_ref, "Expected pull request base ref is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request targets a different base branch than the batch landing plan.",
                status_code=409,
            )
        if base_sha != _required_value(
            expected_base_sha, "Expected pull request base SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request base moved outside the batch landing plan.", status_code=409
            )

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        repository_path = _repository_path(repository)
        normalized_label = _required_value(label, "GitHub label is required.")
        self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/labels",
            body={"labels": [normalized_label]},
        )

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        repository_path = _repository_path(repository)
        self.transport.request(
            method="PUT",
            path=f"/repos/{repository_path}/pulls/{pull_request_number}/update-branch",
            body={
                "expected_head_sha": _required_value(
                    expected_head_sha, "Expected pull request head SHA is required."
                )
            },
        )

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: MergeTrainMergeMethod,
    ) -> str:
        repository_path = _repository_path(repository)
        payload = self.transport.request(
            method="PUT",
            path=f"/repos/{repository_path}/pulls/{pull_request_number}/merge",
            body={
                "sha": _required_value(head_sha, "Pull request head SHA is required."),
                "merge_method": merge_method,
            },
        )
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError(
                "GitHub merge response must be a JSON object.", status_code=None
            )
        merge_commit_sha = str(payload.get("sha") or "").strip()
        if not merge_commit_sha:
            raise MergeTrainGitHubError(
                "GitHub merge response did not include a merge commit SHA.", status_code=None
            )
        return merge_commit_sha

    def comment_pull_request(self, *, repository: str, pull_request_number: int, body: str) -> str:
        repository_path = _repository_path(repository)
        payload = self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/comments",
            body={"body": _required_value(body, "GitHub pull request comment body is required.")},
        )
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError("GitHub comment response must be a JSON object.")
        return str(payload.get("html_url") or "").strip()

    def close_pull_request(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        repository_path = _repository_path(repository)
        pull_request_path = f"/repos/{repository_path}/pulls/{pull_request_number}"
        expected_sha = _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        )
        current_head_sha = self._pull_request_head_sha(pull_request_path=pull_request_path)
        if current_head_sha != _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR moved outside the stored collapse plan.",
                status_code=409,
            )
        closed_pull_request = _json_object(
            self.transport.request(
                method="PATCH",
                path=pull_request_path,
                body={"state": "closed"},
            ),
            "GitHub close pull request response",
        )
        closed_head = _json_object(closed_pull_request.get("head"), "GitHub pull request head")
        closed_head_sha = _required_text(
            closed_head.get("sha"), "GitHub pull request head requires sha."
        )
        if closed_head_sha != expected_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR moved while Launchplane was closing it.",
                status_code=409,
            )
        if _pull_request_state(str(closed_pull_request.get("state") or "")) != "closed":
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR did not remain closed.",
                status_code=409,
            )

    def merge_stack_child_into_parent(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        protected_base_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str:
        repository_path = _repository_path(repository)
        normalized_parent_ref = _required_value(
            parent_head_ref, "Stack collapse parent head ref is required."
        )
        if normalized_parent_ref == _required_value(
            protected_base_ref, "Stack collapse protected base ref is required."
        ):
            raise MergeTrainGitHubError("Stack collapse cannot mutate a protected base branch.")
        current_parent_sha = _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=normalized_parent_ref,
        )
        expected_sha = _required_value(
            expected_parent_head_sha, "Stack collapse expected parent SHA is required."
        )
        if current_parent_sha != expected_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Stack collapse parent branch moved outside the stored plan.",
                status_code=409,
            )
        payload = self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/merges",
            body={
                "base": normalized_parent_ref,
                "head": _required_value(
                    child_head_sha, "Stack collapse child head SHA is required."
                ),
                "commit_message": _stack_collapse_commit_message(
                    collapse_id=collapse_id,
                    child_pull_request_number=child_pull_request_number,
                    parent_pull_request_number=parent_pull_request_number,
                ),
            },
        )
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError("GitHub stack merge response must be a JSON object.")
        return _required_text(payload.get("sha"), "GitHub stack merge response requires sha.")

    def find_stack_child_merge_commit(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str:
        repository_path = _repository_path(repository)
        current_parent_sha = _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=_required_value(
                parent_head_ref,
                "Stack collapse parent head ref is required.",
            ),
        )
        expected_parent_sha = _required_value(
            expected_parent_head_sha,
            "Stack collapse expected parent SHA is required.",
        )
        if current_parent_sha == expected_parent_sha:
            return ""
        payload = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/commits/{quote(current_parent_sha, safe='')}",
            ),
            "GitHub commit response",
        )
        commit = _json_object(payload.get("commit"), "GitHub commit detail")
        message = str(commit.get("message") or "").strip()
        parents = payload.get("parents")
        parent_shas = (
            {str(parent.get("sha") or "").strip() for parent in parents if isinstance(parent, dict)}
            if isinstance(parents, list)
            else set()
        )
        expected_message = _stack_collapse_commit_message(
            collapse_id=collapse_id,
            child_pull_request_number=child_pull_request_number,
            parent_pull_request_number=parent_pull_request_number,
        )
        if message == expected_message and parent_shas == {
            expected_parent_sha,
            _required_value(child_head_sha, "Stack collapse child head SHA is required."),
        }:
            return current_parent_sha
        raise MergeTrainGitHubStaleHeadError(
            "Stack collapse parent branch moved outside the stored plan.",
            status_code=409,
        )

    def _create_or_reset_reference(self, *, repository_path: str, reference: str, sha: str) -> None:
        normalized_sha = _required_value(sha, "GitHub reference SHA is required.")
        try:
            self.transport.request(
                method="POST",
                path=f"/repos/{repository_path}/git/refs",
                body={"ref": reference, "sha": normalized_sha},
            )
        except MergeTrainGitHubError as error:
            if error.status_code != 409:
                raise
            reference_path = _reference_path(reference)
            self.transport.request(
                method="PATCH",
                path=f"/repos/{repository_path}/git/refs/{reference_path}",
                body={"sha": normalized_sha, "force": True},
            )

    def _delete_reference_if_present(self, *, repository_path: str, reference: str) -> bool:
        try:
            self.transport.request(
                method="DELETE",
                path=f"/repos/{repository_path}/git/refs/{_reference_path(reference)}",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    def _pull_request_head_sha(self, *, pull_request_path: str) -> str:
        pull_request = _json_object(
            self.transport.request(method="GET", path=pull_request_path),
            "GitHub pull request detail response",
        )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        return _required_text(head.get("sha"), "GitHub pull request head requires sha.")


class GitHubMergeTrainSnapshotReader:
    def __init__(self, *, transport: MergeTrainGitHubTransport) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        repository_path = _repository_path(repository)
        normalized_base_branch = _required_value(
            base_branch, "GitHub pull request base branch is required."
        )
        base_sha = self._base_branch_sha(
            repository_path=repository_path, base_branch=normalized_base_branch
        )
        open_pull_requests = self._list_open_pull_requests(repository_path=repository_path)
        relevant_pull_requests = _base_rooted_pull_requests(
            pull_requests=open_pull_requests,
            repository=repository,
            base_branch=normalized_base_branch,
        )
        pull_requests = tuple(
            self._pull_request_snapshot(
                repository=repository,
                repository_path=repository_path,
                pull_request=pull_request,
            )
            for pull_request in relevant_pull_requests
        )
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=normalized_base_branch,
            base_sha=base_sha,
            pull_requests=pull_requests,
        )

    def _base_branch_sha(self, *, repository_path: str, base_branch: str) -> str:
        return _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=base_branch,
        )

    def _list_open_pull_requests(self, *, repository_path: str) -> tuple[dict[str, object], ...]:
        pull_requests: list[dict[str, object]] = []
        page = 1
        while True:
            query = urlencode(
                {
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": "100",
                    "page": str(page),
                }
            )
            payload = self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls?{query}"
            )
            if not isinstance(payload, list):
                raise MergeTrainGitHubError(
                    "GitHub pull request list response must be a JSON array."
                )
            page_pull_requests = [
                _json_object(item, "GitHub pull request entry") for item in payload
            ]
            pull_requests.extend(page_pull_requests)
            if len(page_pull_requests) < 100:
                return tuple(pull_requests)
            page += 1

    def _pull_request_snapshot(
        self, *, repository: str, repository_path: str, pull_request: dict[str, object]
    ) -> MergeTrainPullRequestSnapshot:
        pull_request_number = _required_int(
            pull_request.get("number"), "GitHub pull request entry requires number."
        )
        detail = _json_object(
            self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls/{pull_request_number}"
            ),
            "GitHub pull request detail response",
        )
        source = pull_request | detail
        head = _json_object(source.get("head"), "GitHub pull request head")
        base = _json_object(source.get("base"), "GitHub pull request base")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        head_repository = _repository_full_name(head.get("repo"), "GitHub pull request head repo")
        base_repository = _repository_full_name(base.get("repo"), "GitHub pull request base repo")
        user = _json_object(source.get("user"), "GitHub pull request user")
        actor_id = _required_int(user.get("id"), "GitHub pull request user requires id.")
        actor_role = self._actor_role_for_pull_request(
            repository_path=repository_path,
            username=_required_text(user.get("login"), "GitHub pull request user requires login."),
            author_association=str(source.get("author_association") or ""),
        )
        return MergeTrainPullRequestSnapshot(
            number=pull_request_number,
            url=str(source.get("html_url") or "").strip(),
            title=str(source.get("title") or "").strip(),
            state=_pull_request_state(str(source.get("state") or "")),
            is_draft=bool(source.get("draft")),
            created_at=_required_text(
                source.get("created_at"), "GitHub pull request entry requires created_at."
            ),
            labels=_labels(source.get("labels")),
            actor_id=actor_id,
            actor_role=actor_role,
            head_sha=head_sha,
            head_ref=_required_text(head.get("ref"), "GitHub pull request head requires ref."),
            head_repository=head_repository,
            base_sha=str(base.get("sha") or "").strip(),
            base_ref=_required_text(base.get("ref"), "GitHub pull request base requires ref."),
            base_repository=base_repository,
            mergeable=_mergeable_state(source),
            required_checks_status=self._required_checks_status(
                repository_path=repository_path, head_sha=head_sha
            ),
            branch_update_required=_branch_update_required(source),
        )

    def _actor_role_for_pull_request(
        self, *, repository_path: str, username: str, author_association: str
    ) -> str:
        if author_association.upper() == "OWNER":
            return "repo_owner"
        try:
            payload = _json_object(
                self.transport.request(
                    method="GET",
                    path=f"/repos/{repository_path}/collaborators/{quote(username, safe='')}/permission",
                ),
                "GitHub collaborator permission response",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return "unknown"
            raise
        return "repo_admin" if str(payload.get("permission") or "") == "admin" else "unknown"

    def _required_checks_status(
        self, *, repository_path: str, head_sha: str
    ) -> MergeTrainCheckStatus:
        encoded_head_sha = quote(head_sha, safe="")
        return _required_checks_status(
            transport=self.transport,
            repository_path=repository_path,
            encoded_head_sha=encoded_head_sha,
        )

    def _list_check_runs(self, *, repository_path: str, encoded_head_sha: str) -> dict[str, object]:
        return _list_check_runs(
            transport=self.transport,
            repository_path=repository_path,
            encoded_head_sha=encoded_head_sha,
        )


class MergeTrainGitHubRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    body: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "MergeTrainGitHubRequest":
        self.method = _required_value(self.method, "GitHub request method is required.").upper()
        self.path = _required_value(self.path, "GitHub request path is required.")
        if not self.path.startswith("/"):
            raise ValueError("GitHub request path must start with '/'.")
        return self


class RecordingMergeTrainGitHubTransport:
    def __init__(self, *, responses: tuple[object, ...] = ()) -> None:
        self.responses = list(responses)
        self.requests: list[MergeTrainGitHubRequest] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.requests.append(
            MergeTrainGitHubRequest.model_validate({"method": method, "path": path, "body": body})
        )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {}


def _repository_path(repository: str) -> str:
    normalized = _required_value(repository, "GitHub repository is required.")
    parts = normalized.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError("GitHub repository must be formatted as owner/name.")
    return "/".join(quote(part.strip(), safe="") for part in parts)


def _branch_name_from_ref(reference: str) -> str:
    normalized = _required_value(reference, "GitHub branch ref is required.")
    prefix = "refs/heads/"
    if not normalized.startswith(prefix):
        raise ValueError("GitHub branch ref must start with refs/heads/.")
    return normalized.removeprefix(prefix)


def _reference_path(reference: str) -> str:
    normalized = _required_value(reference, "GitHub reference is required.")
    prefix = "refs/"
    if not normalized.startswith(prefix):
        raise ValueError("GitHub reference must start with refs/.")
    return "/".join(quote(part, safe="") for part in normalized.removeprefix(prefix).split("/"))


def _json_object(value: object, label: str) -> dict[str, object]:
    return json_object(value, label, error_type=MergeTrainGitHubError)


def _repository_full_name(value: object, label: str) -> str:
    repository = _json_object(value, label)
    return _required_text(repository.get("full_name"), f"{label} requires full_name.")


def _base_rooted_pull_requests(
    *,
    pull_requests: tuple[dict[str, object], ...],
    repository: str,
    base_branch: str,
) -> tuple[dict[str, object], ...]:
    by_base_ref: dict[str, list[dict[str, object]]] = {}
    relevant_numbers: set[int] = set()
    for pull_request in pull_requests:
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        base_repository = _repository_full_name(base.get("repo"), "GitHub pull request base repo")
        if base_repository != repository:
            continue
        base_ref = _required_text(base.get("ref"), "GitHub pull request base requires ref.")
        by_base_ref.setdefault(base_ref, []).append(pull_request)

    branch_refs = [base_branch]
    seen_branch_refs: set[str] = set()
    while branch_refs:
        branch_ref = branch_refs.pop(0)
        if branch_ref in seen_branch_refs:
            continue
        seen_branch_refs.add(branch_ref)
        for pull_request in by_base_ref.get(branch_ref, ()):
            pull_request_number = _required_int(
                pull_request.get("number"), "GitHub pull request entry requires number."
            )
            if pull_request_number in relevant_numbers:
                continue
            relevant_numbers.add(pull_request_number)
            head = _json_object(pull_request.get("head"), "GitHub pull request head")
            head_repository = _repository_full_name(
                head.get("repo"), "GitHub pull request head repo"
            )
            if head_repository != repository:
                continue
            branch_refs.append(
                _required_text(head.get("ref"), "GitHub pull request head requires ref.")
            )
    return tuple(
        pull_request
        for pull_request in pull_requests
        if _required_int(pull_request.get("number"), "GitHub pull request entry requires number.")
        in relevant_numbers
    )


def _required_int(value: object, message: str) -> int:
    return required_positive_int(value, message, error_type=MergeTrainGitHubError)


def _required_text(value: object, message: str) -> str:
    return required_string_text(value, message, error_type=MergeTrainGitHubError)


def _pull_request_state(value: str) -> MergeTrainPullRequestState:
    normalized = value.strip().lower()
    if normalized == "open":
        return "open"
    if normalized == "closed":
        return "closed"
    raise MergeTrainGitHubError("GitHub pull request state must be open or closed.")


def _labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MergeTrainGitHubError("GitHub pull request labels must be a JSON array.")
    labels: list[str] = []
    for item in value:
        label = _json_object(item, "GitHub pull request label")
        name = _required_text(label.get("name"), "GitHub pull request label requires name.")
        if name not in labels:
            labels.append(name)
    return tuple(labels)


def _mergeable_state(source: dict[str, object]) -> MergeTrainMergeableState:
    if bool(source.get("draft")):
        return "unknown"
    mergeable = source.get("mergeable")
    if mergeable is True:
        return "mergeable"
    if mergeable is False:
        return "conflicting"
    return "unknown"


def _branch_update_required(source: dict[str, object]) -> bool:
    mergeable_state = str(source.get("mergeable_state") or "").strip().lower()
    return mergeable_state == "behind"


def _base_branch_sha(
    *, transport: MergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> str:
    branch = _json_object(
        transport.request(
            method="GET",
            path=f"/repos/{repository_path}/branches/{quote(base_branch, safe='')}",
        ),
        "GitHub branch response",
    )
    commit = _json_object(branch.get("commit"), "GitHub branch commit")
    return _required_text(commit.get("sha"), "GitHub branch commit requires sha.")


def _combined_status_state(payload: dict[str, object]) -> MergeTrainCheckStatus:
    raw_statuses = payload.get("statuses")
    if not isinstance(raw_statuses, list):
        raise MergeTrainGitHubError("GitHub combined status response must include statuses.")
    statuses: list[MergeTrainCheckStatus] = []
    seen_contexts: set[str] = set()
    for item in raw_statuses:
        status = _json_object(item, "GitHub commit status")
        context = _required_text(
            status.get("context"), "GitHub commit status requires context."
        )
        normalized_context = context.casefold()
        if (
            is_launchplane_projected_check(context)
            or normalized_context in seen_contexts
        ):
            continue
        seen_contexts.add(normalized_context)
        statuses.append(_commit_status_state(status))
    if not statuses:
        return "unknown"
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "pending" for status in statuses):
        return "pending"
    if all(status == "pass" for status in statuses):
        return "pass"
    return "unknown"


def _commit_status_state(payload: dict[str, object]) -> MergeTrainCheckStatus:
    state = _required_text(
        payload.get("state"), "GitHub commit status requires state."
    ).lower()
    if state == "success":
        return "pass"
    if state in {"failure", "error"}:
        return "fail"
    if state == "pending":
        return "pending"
    return "unknown"


def _check_runs_status(payload: dict[str, object]) -> MergeTrainCheckStatus:
    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
    if not check_runs:
        return "unknown"
    statuses: list[MergeTrainCheckStatus] = []
    for item in check_runs:
        check_run = _json_object(item, "GitHub check run")
        if is_launchplane_projected_check(str(check_run.get("name") or "")):
            continue
        statuses.append(_check_run_status(check_run))
    if not statuses:
        return "unknown"
    return _combine_check_statuses(*statuses)


def _required_checks_status(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    encoded_head_sha: str,
) -> MergeTrainCheckStatus:
    status_payload = _list_commit_statuses(
        transport=transport,
        repository_path=repository_path,
        encoded_head_sha=encoded_head_sha,
    )
    check_runs_payload = _list_check_runs(
        transport=transport,
        repository_path=repository_path,
        encoded_head_sha=encoded_head_sha,
    )
    return _combine_check_statuses(
        _combined_status_state(status_payload), _check_runs_status(check_runs_payload)
    )


def _list_commit_statuses(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    encoded_head_sha: str,
) -> dict[str, object]:
    statuses: list[object] = []
    total_count: int | None = None
    page = 1
    while True:
        payload = _json_object(
            transport.request(
                method="GET",
                path=(
                    f"/repos/{repository_path}/commits/{encoded_head_sha}/status"
                    f"?per_page=100&page={page}"
                ),
            ),
            "GitHub combined status response",
        )
        raw_statuses = payload.get("statuses")
        if not isinstance(raw_statuses, list):
            raise MergeTrainGitHubError(
                "GitHub combined status response must include statuses."
            )
        raw_total_count = payload.get("total_count")
        if isinstance(raw_total_count, int):
            total_count = raw_total_count
        statuses.extend(raw_statuses)
        if len(raw_statuses) < 100 or (
            total_count is not None and len(statuses) >= total_count
        ):
            break
        page += 1
    return {
        "total_count": total_count if total_count is not None else len(statuses),
        "statuses": statuses,
    }


def _list_check_runs(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    encoded_head_sha: str,
) -> dict[str, object]:
    check_runs: list[object] = []
    page = 1
    total_count: int | None = None
    while True:
        query = urlencode({"per_page": "100", "page": str(page)})
        payload = _json_object(
            transport.request(
                method="GET",
                path=(f"/repos/{repository_path}/commits/{encoded_head_sha}/check-runs?{query}"),
            ),
            "GitHub check runs response",
        )
        raw_check_runs = payload.get("check_runs")
        if not isinstance(raw_check_runs, list):
            raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
        raw_total_count = payload.get("total_count")
        if isinstance(raw_total_count, int):
            total_count = raw_total_count
        check_runs.extend(raw_check_runs)
        if len(raw_check_runs) < 100:
            break
        page += 1
    return {
        "total_count": total_count if total_count is not None else len(check_runs),
        "check_runs": check_runs,
    }


def _check_run_status(check_run: dict[str, object]) -> MergeTrainCheckStatus:
    status = str(check_run.get("status") or "").strip().lower()
    if status != "completed":
        return "pending" if status else "unknown"
    conclusion = str(check_run.get("conclusion") or "").strip().lower()
    if conclusion in {"success", "neutral", "skipped"}:
        return "pass"
    if conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
        return "fail"
    return "unknown"


def _combine_check_statuses(*statuses: MergeTrainCheckStatus) -> MergeTrainCheckStatus:
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "pending" for status in statuses):
        return "pending"
    if statuses and all(status == "pass" for status in statuses):
        return "pass"
    if any(status == "pass" for status in statuses):
        return "pass"
    return "unknown"


def _stack_collapse_commit_message(
    *,
    collapse_id: str,
    child_pull_request_number: int,
    parent_pull_request_number: int,
) -> str:
    return (
        f"Launchplane stack collapse {collapse_id}: merge PR "
        f"#{child_pull_request_number} into PR #{parent_pull_request_number}"
    )


def _required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _github_http_error(*, path: str, status_code: int, error: HTTPError) -> MergeTrainGitHubError:
    message = f"GitHub API request failed for {path}: HTTP {status_code}"
    if status_code == 409:
        return MergeTrainGitHubStaleHeadError(message, status_code=status_code)
    return MergeTrainGitHubError(message, status_code=status_code)
