from __future__ import annotations

from contextlib import nullcontext
from collections import Counter
from pathlib import Path
import re
import unittest

from control_plane.repository_evidence import (
    RepositoryEvidenceError,
    RepositoryEvidenceStaleError,
    GitHubRepositoryEvidenceProvider,
)
from control_plane.contracts.repository_evidence import (
    RepositoryTargetReference,
)


REPOSITORY = "example/shared-addons"
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
BASE_SHA = "d" * 40
MERGE_SHA = "e" * 40
UPDATED_AT = "2026-08-06T01:00:00Z"
BASE_REF = "main"
PULL_REQUEST_AUTHOR_GITHUB_ID = 3001
COMMIT_AUTHOR_GITHUB_ID = 3002


def _repository_payload() -> dict[str, object]:
    return {
        "id": 1001,
        "full_name": REPOSITORY,
        "owner": {"id": 2001},
    }


def _pull_request_payload(
    head_sha: str = HEAD_SHA,
    *,
    base_sha: str = BASE_SHA,
    merge_commit_sha: str = MERGE_SHA,
    updated_at: str = UPDATED_AT,
    author_type: str = "User",
) -> dict[str, object]:
    return {
        "base": {
            "sha": base_sha,
            "ref": BASE_REF,
            "repo": {"id": 1001, "full_name": REPOSITORY},
        },
        "head": {"sha": head_sha},
        "merge_commit_sha": merge_commit_sha,
        "updated_at": updated_at,
        "user": {
            "id": PULL_REQUEST_AUTHOR_GITHUB_ID,
            "login": "review-author",
            "type": author_type,
        },
    }


def _commit_payload(
    *,
    sha: str = "f" * 40,
    author_id: int | None = COMMIT_AUTHOR_GITHUB_ID,
    author_login: str = "commit-author",
    author_type: str = "User",
    committer_id: int | None = COMMIT_AUTHOR_GITHUB_ID,
    committer_login: str = "commit-author",
    committer_type: str = "User",
) -> dict[str, object]:
    payload: dict[str, object] = {"sha": sha}
    payload["author"] = (
        {"id": author_id, "login": author_login, "type": author_type} if author_id else None
    )
    payload["committer"] = (
        {"id": committer_id, "login": committer_login, "type": committer_type}
        if committer_id
        else None
    )
    return payload


class _GitHubApi:
    def __init__(
        self,
        *,
        confirmed_head_sha: str = HEAD_SHA,
        confirmed_base_sha: str = BASE_SHA,
        confirmed_merge_commit_sha: str = MERGE_SHA,
        confirmed_updated_at: str = UPDATED_AT,
        pull_request_author_type: str = "User",
        commits: tuple[dict[str, object], ...] | None = None,
    ) -> None:
        self.confirmed_head_sha = confirmed_head_sha
        self.confirmed_base_sha = confirmed_base_sha
        self.confirmed_merge_commit_sha = confirmed_merge_commit_sha
        self.confirmed_updated_at = confirmed_updated_at
        self.pull_request_author_type = pull_request_author_type
        self.commits = commits if commits is not None else (_commit_payload(),)
        self.pull_request_reads = 0
        self.paths: list[str] = []
        self.revoked: list[str] = []

    def __call__(self, *, path: str, token: str, method: str = "GET") -> object:
        if path == "/installation/token" and method == "DELETE":
            self.revoked.append(token)
            return None
        self.paths.append(path)
        self.last_token = token
        if path == "/repos/example/shared-addons":
            return _repository_payload()
        if path == "/repos/example/shared-addons/pulls/2000":
            self.pull_request_reads += 1
            if self.pull_request_reads == 1:
                return _pull_request_payload(author_type=self.pull_request_author_type)
            return _pull_request_payload(
                self.confirmed_head_sha,
                base_sha=self.confirmed_base_sha,
                merge_commit_sha=self.confirmed_merge_commit_sha,
                updated_at=self.confirmed_updated_at,
                author_type=self.pull_request_author_type,
            )
        if (
            path == "/repos/example/shared-addons/pulls?state=open&sort=updated&direction=desc"
            "&per_page=20&page=1"
        ):
            return [
                {
                    "number": 2000,
                    "title": "Current Owner acceptance candidate",
                    "html_url": "https://github.com/example/shared-addons/pull/2000",
                    "updated_at": UPDATED_AT,
                }
            ]
        if path == f"/repos/example/shared-addons/git/commits/{HEAD_SHA}":
            return {"tree": {"sha": TREE_SHA}}
        if path.startswith("/repos/example/shared-addons/pulls/2000/commits?"):
            return list(self.commits) if "page=1" in path else []
        if path == "/repos/example/shared-addons/pulls/2000/files?per_page=100&page=1":
            return [
                {
                    "filename": "src/runtime/app.py",
                    "status": "modified",
                },
                {
                    "filename": "docs/new-name.md",
                    "previous_filename": "control_plane/service_auth.py",
                    "status": "renamed",
                },
            ]
        raise AssertionError(f"unexpected GitHub path {path}")


class _RepeatingEvidenceApi(_GitHubApi):
    def __init__(self) -> None:
        super().__init__()
        self.repository_id = 1001
        self.base_sha = BASE_SHA
        self.head_suffix = 0
        self.confirmation_changes = False
        self.tree_unavailable = False

    def __call__(self, *, path: str, token: str, method: str = "GET") -> object:
        if path == f"/repos/{REPOSITORY}":
            self.paths.append(path)
            return {**_repository_payload(), "id": self.repository_id}
        if re.fullmatch(rf"/repos/{REPOSITORY}/pulls/\d+", path):
            self.paths.append(path)
            self.pull_request_reads += 1
            number = int(path.rsplit("/", 1)[1])
            suffix = self.head_suffix
            if self.confirmation_changes and self.pull_request_reads % 2 == 0:
                suffix += 1
            payload = _pull_request_payload(f"{number + suffix:040x}", base_sha=self.base_sha)
            payload["base"] = {
                "sha": self.base_sha,
                "ref": BASE_REF,
                "repo": {"id": self.repository_id, "full_name": REPOSITORY},
            }
            return payload
        if "/git/commits/" in path:
            self.paths.append(path)
            if self.tree_unavailable:
                return {"tree": {}}
            return {"tree": {"sha": path.rsplit("/", 1)[1]}}
        normalized_path = re.sub(r"/pulls/\d+/", "/pulls/2000/", path)
        return super().__call__(path=normalized_path, token=token, method=method)


class RepeatedRepositoryEvidenceTests(unittest.TestCase):
    def _provider(
        self, api: _RepeatingEvidenceApi, *, reuse_commit_trees: bool = True
    ) -> GitHubRepositoryEvidenceProvider:
        return GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "train-token",
            github_api=api,
            github_token_scope=lambda **_: nullcontext("train-token"),
            token_context="launchplane",
            reuse_commit_trees=reuse_commit_trees,
        )

    def _target(self, number: int = 2000) -> RepositoryTargetReference:
        return RepositoryTargetReference(repository=REPOSITORY, pull_request_number=number)

    def test_repeated_batch_evidence_reuses_only_immutable_tree_reads(self) -> None:
        members = tuple(self._target(number) for number in range(2000, 2007))
        # Protected-batch preflight and admission each evaluate every member.
        evaluations = 2 * len(members)
        baseline_api, cached_api = _RepeatingEvidenceApi(), _RepeatingEvidenceApi()
        baseline = self._provider(baseline_api, reuse_commit_trees=False)
        cached = self._provider(cached_api)
        for _ in range(evaluations):
            for member in members:
                self.assertEqual(cached.resolve(member), baseline.resolve(member))

        def tree_path(path: str) -> bool:
            return "/git/commits/" in path

        self.assertEqual(
            sum(tree_path(path) for path in baseline_api.paths), evaluations * len(members)
        )
        self.assertEqual(sum(tree_path(path) for path in cached_api.paths), len(members))
        self.assertEqual(
            Counter(path for path in cached_api.paths if not tree_path(path)),
            Counter(path for path in baseline_api.paths if not tree_path(path)),
        )
        self.assertEqual(cached_api.revoked, [])

    def test_changed_head_and_repository_identity_require_new_tree_reads(self) -> None:
        api = _RepeatingEvidenceApi()
        provider = self._provider(api)
        first = provider.resolve(self._target())
        api.head_suffix += 1
        second = provider.resolve(self._target())
        self.assertNotEqual(first.target.tree_sha, second.target.tree_sha)
        api.repository_id += 1
        third = provider.resolve(self._target())
        self.assertNotEqual(second.target.repository_id, third.target.repository_id)
        self.assertEqual(sum("/git/commits/" in path for path in api.paths), 3)

    def test_base_files_and_authorship_stay_fresh_with_a_warm_tree(self) -> None:
        api = _RepeatingEvidenceApi()
        provider = self._provider(api)
        first = provider.resolve(self._target())
        api.base_sha = "c" * 40
        api.commits = (_commit_payload(author_id=None, committer_id=None),)
        second = provider.resolve(self._target())
        self.assertNotEqual(first.base, second.base)
        assert second.authorship is not None
        self.assertEqual(second.authorship.resolution, "unresolved")
        self.assertEqual(sum("/git/commits/" in path for path in api.paths), 1)
        self.assertEqual(sum("/files?" in path for path in api.paths), 2)
        self.assertEqual(api.pull_request_reads, 4)

    def test_confirmation_still_refuses_a_changed_head_with_a_warm_tree(self) -> None:
        api = _RepeatingEvidenceApi()
        provider = self._provider(api)
        provider.resolve(self._target())
        api.confirmation_changes = True
        with self.assertRaises(RepositoryEvidenceStaleError):
            provider.resolve(self._target())
        self.assertEqual(sum("/git/commits/" in path for path in api.paths), 1)

    def test_failed_tree_reads_are_retried_and_new_readers_start_empty(self) -> None:
        api = _RepeatingEvidenceApi()
        provider = self._provider(api)
        api.tree_unavailable = True
        for _ in range(2):
            with self.assertRaises(RepositoryEvidenceError):
                provider.resolve(self._target())
        api.tree_unavailable = False
        provider.resolve(self._target())
        provider.resolve(self._target())
        self._provider(api).resolve(self._target())
        self.assertEqual(sum("/git/commits/" in path for path in api.paths), 4)


class ChangeImpactGitHubEvidenceProviderTests(unittest.TestCase):
    def test_borrowed_train_token_remains_owned_by_the_train(self) -> None:
        github = _GitHubApi()
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **kwargs: "train-token",
            github_api=github,
            token_context="launchplane",
            github_token_scope=lambda **kwargs: nullcontext("train-token"),
        )
        provider.list_open_pull_requests(REPOSITORY, limit=20)
        self.assertEqual(github.last_token, "train-token")
        self.assertEqual(github.revoked, [])

    def test_lists_bounded_open_pull_requests(self) -> None:
        github_api = _GitHubApi()
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=github_api,
            token_context="launchplane",
        )

        pull_requests = provider.list_open_pull_requests(REPOSITORY, limit=20)

        self.assertEqual(len(pull_requests), 1)
        self.assertEqual(pull_requests[0].repository, REPOSITORY)
        self.assertEqual(pull_requests[0].pull_request_number, 2000)
        self.assertEqual(pull_requests[0].title, "Current Owner acceptance candidate")
        self.assertEqual(github_api.last_token, "server-token")
        self.assertEqual(github_api.revoked, ["server-token"])

    def test_resolves_repository_head_tree_and_complete_changed_paths(self) -> None:
        github_api = _GitHubApi()
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=github_api,
            token_context="launchplane",
        )

        evidence = provider.resolve(
            RepositoryTargetReference(
                repository=REPOSITORY,
                pull_request_number=2000,
            )
        )

        self.assertEqual(evidence.target.repository_id, "1001")
        self.assertEqual(evidence.target.repository_owner_id, "2001")
        self.assertEqual(evidence.target.head_sha, HEAD_SHA)
        self.assertEqual(evidence.target.tree_sha, TREE_SHA)
        self.assertEqual(evidence.merge_commit_sha, MERGE_SHA)
        self.assertEqual(
            tuple(file.path for file in evidence.changed_files),
            (
                "control_plane/service_auth.py",
                "docs/new-name.md",
                "src/runtime/app.py",
            ),
        )
        self.assertEqual(github_api.last_token, "server-token")
        self.assertEqual(github_api.revoked, ["server-token"])
        self.assertEqual(github_api.pull_request_reads, 2)
        assert evidence.authorship is not None
        self.assertEqual(evidence.authorship.resolution, "resolved")
        self.assertEqual(
            evidence.authorship.contributor_github_ids,
            (PULL_REQUEST_AUTHOR_GITHUB_ID, COMMIT_AUTHOR_GITHUB_ID),
        )

    def test_non_human_commit_actor_keeps_authorship_unresolved(self) -> None:
        github_api = _GitHubApi(
            commits=(
                _commit_payload(
                    author_type="Bot",
                    committer_type="Bot",
                ),
            )
        )
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=github_api,
            token_context="launchplane",
        )

        evidence = provider.resolve(
            RepositoryTargetReference(
                repository=REPOSITORY,
                pull_request_number=2000,
            )
        )

        assert evidence.authorship is not None
        self.assertEqual(evidence.authorship.resolution, "unresolved")
        self.assertEqual(evidence.authorship.contributor_github_ids, ())

    def test_non_human_pull_request_author_keeps_authorship_unresolved(self) -> None:
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=_GitHubApi(pull_request_author_type="Bot"),
            token_context="launchplane",
        )

        evidence = provider.resolve(
            RepositoryTargetReference(
                repository=REPOSITORY,
                pull_request_number=2000,
            )
        )

        assert evidence.authorship is not None
        self.assertEqual(evidence.authorship.resolution, "unresolved")
        self.assertEqual(evidence.authorship.contributor_github_ids, ())

    def test_bot_authored_commit_pushed_by_human_resolves_to_human(self) -> None:
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=_GitHubApi(
                commits=(
                    _commit_payload(
                        author_type="Bot",
                        committer_id=COMMIT_AUTHOR_GITHUB_ID,
                        committer_login="commit-author",
                        committer_type="User",
                    ),
                )
            ),
            token_context="launchplane",
        )

        evidence = provider.resolve(
            RepositoryTargetReference(
                repository=REPOSITORY,
                pull_request_number=2000,
            )
        )

        assert evidence.authorship is not None
        self.assertEqual(evidence.authorship.resolution, "resolved")
        self.assertEqual(
            evidence.authorship.contributor_github_ids,
            (PULL_REQUEST_AUTHOR_GITHUB_ID, COMMIT_AUTHOR_GITHUB_ID),
        )

    def test_head_change_during_resolution_is_stale(self) -> None:
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=_GitHubApi(confirmed_head_sha="c" * 40),
            token_context="launchplane",
        )

        with self.assertRaises(RepositoryEvidenceStaleError):
            provider.resolve(
                RepositoryTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2000,
                )
            )

    def test_pull_request_update_during_resolution_is_stale(self) -> None:
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=_GitHubApi(confirmed_updated_at="2026-08-06T01:00:01Z"),
            token_context="launchplane",
        )

        with self.assertRaises(RepositoryEvidenceStaleError):
            provider.resolve(
                RepositoryTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2000,
                )
            )

    def test_missing_server_credentials_fail_closed(self) -> None:
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "",
            github_api=_GitHubApi(),
            token_context="launchplane",
        )

        with self.assertRaises(RepositoryEvidenceError):
            provider.resolve(
                RepositoryTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2000,
                )
            )

    def test_incomplete_file_pagination_fails_closed(self) -> None:
        class FullPagesGitHubApi(_GitHubApi):
            def __call__(self, *, path: str, token: str, method: str = "GET") -> object:
                if "/files?" in path:
                    return [
                        {"filename": f"path-{index}.py", "status": "modified"}
                        for index in range(100)
                    ]
                return super().__call__(path=path, token=token, method=method)

        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=FullPagesGitHubApi(),
            token_context="launchplane",
            max_file_pages=1,
        )

        with self.assertRaises(RepositoryEvidenceError):
            provider.resolve(
                RepositoryTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2000,
                )
            )

    def test_current_item_resolution_uses_smaller_file_page_bound(self) -> None:
        class FullPagesGitHubApi(_GitHubApi):
            def __call__(self, *, path: str, token: str, method: str = "GET") -> object:
                if "/files?" in path:
                    self.paths.append(path)
                    page = path.rsplit("=", 1)[-1]
                    return [
                        {"filename": f"path-{page}-{index}.py", "status": "modified"}
                        for index in range(100)
                    ]
                return super().__call__(path=path, token=token, method=method)

        github_api = FullPagesGitHubApi()
        provider = GitHubRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "server-token",
            github_api=github_api,
            token_context="launchplane",
            max_file_pages=30,
        )

        with self.assertRaises(RepositoryEvidenceError):
            provider.resolve_current_item(
                RepositoryTargetReference(
                    repository=REPOSITORY,
                    pull_request_number=2000,
                )
            )

        self.assertEqual(sum("/files?" in path for path in github_api.paths), 5)


if __name__ == "__main__":
    unittest.main()
