import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
    build_merge_train_branch_refresh_record,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.merge_train_branch_refresh import merge_train_branch_refresh_recorder
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.product_review import require_product_review_store
from control_plane.product_review_status import OwnerReviewStatusPublisher
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url
from tests.test_product_review_status import _app_token

_REPOSITORY = "every/example-site"
_PULL_REQUEST = 92
_ACCEPTED_HEAD = "bd92ceed" + "0" * 32
_REFRESHED_HEAD = "b064ecdc" + "0" * 32
_SECOND_REFRESH_HEAD = "c0ffee00" + "0" * 32
# Another merge of the same two parents with the same change, pushed by someone else.
_OTHER_MERGE_HEAD = "d00dfeed" + "0" * 32
_BASE_COMMIT = "1111111111111111111111111111111111111111"
_NEWER_BASE_COMMIT = "2222222222222222222222222222222222222222"
_OWNER_GITHUB_ID = "9001"
_REFRESH_REQUESTED_AT = datetime(2026, 10, 2, 23, 20, tzinfo=timezone.utc)
_PATCH = "@@ -1 +1 @@\n-Old headline\n+New headline"


def _change(*, patch: str = _PATCH, blob: str = "a" * 40) -> list[dict[str, object]]:
    return [
        {
            "filename": "website/views/home.xml",
            "status": "modified",
            "sha": blob,
            "changes": 2,
            "patch": patch,
        }
    ]


class _GitHub:
    """A marked pull request and its comparisons as GitHub would serve them."""

    def __init__(self) -> None:
        self.head_sha = _REFRESHED_HEAD
        self.base_branch = "main"
        # Commits on the base branch, and each head's change against a base commit.
        self.on_base = {_BASE_COMMIT, _NEWER_BASE_COMMIT}
        self.changes: dict[tuple[str, str], list[dict[str, object]]] = {
            (_BASE_COMMIT, head): _change()
            for head in (_ACCEPTED_HEAD, _REFRESHED_HEAD, _OTHER_MERGE_HEAD)
        }
        self.statuses: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        path: str,
        token: str,
        method: str = "GET",
        body: dict[str, object] | None = None,
    ) -> object:
        repository_path = f"/repos/{_REPOSITORY}"
        if path == f"{repository_path}/pulls/{_PULL_REQUEST}":
            return {
                "head": {"sha": self.head_sha},
                "labels": [{"name": "owner-review"}],
                "base": {"ref": self.base_branch, "repo": {"id": 5150}},
            }
        if path == f"{repository_path}/statuses/{self.head_sha}" and method == "POST":
            assert token and body is not None
            self.statuses.insert(0, dict(body))
            return body
        if path == "/installation/token" and method == "DELETE":
            return None
        if "/check-runs?" in path:
            return {"check_runs": list(self.check_runs)}
        if path == f"{repository_path}/check-runs" and method == "POST":
            assert body is not None
            run = {"id": len(self.check_runs) + 1, "app": {"id": 77}, **body}
            self.check_runs.insert(0, run)
            self._record_check(run)
            return run
        if path.startswith(f"{repository_path}/check-runs/") and method == "PATCH":
            assert body is not None
            run = next(run for run in self.check_runs if run["id"] == int(path.rsplit("/", 1)[1]))
            run.update(body)
            self._record_check(run)
            return run
        if path.startswith(f"{repository_path}/compare/"):
            base, head = path.rsplit("/", 1)[1].split("...")
            if head == self.base_branch:
                return {"status": "ahead" if base in self.on_base else "diverged"}
            return {"status": "diverged", "files": self.changes[(base, head)]}
        raise AssertionError(f"Unexpected GitHub request: {method} {path}")

    def _record_check(self, run: dict[str, object]) -> None:
        output = run["output"]
        assert isinstance(output, dict)
        self.statuses.insert(
            0,
            {
                "state": "pending" if run["status"] == "in_progress" else run["conclusion"],
                "description": output["title"],
            },
        )


class CarryOwnerAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary_directory.name))
        self.github = _GitHub()
        payload = product_profile_payload("example-site")
        payload["repository"] = _REPOSITORY
        payload["preview"] = {"enabled": True, "context": "example-site"}
        payload["owner"] = {"github_login": "site-owner", "github_id": _OWNER_GITHUB_ID}
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.accepted = ProductReviewDecisionRecord(
            record_id="decision-accepted",
            feedback_requested=True,
            feedback_url=f"https://github.com/{_REPOSITORY}/pull/{_PULL_REQUEST}#issuecomment-1",
            product="example-site",
            repository=_REPOSITORY,
            pull_request_number=_PULL_REQUEST,
            head_sha=_ACCEPTED_HEAD,
            preview_url="https://pr-92.example.invalid",
            decision="accepted",
            owner_github_id=_OWNER_GITHUB_ID,
            owner_github_login="site-owner",
            decided_at="2026-10-02T22:00:00.000Z",
            base_branch="main",
        )
        self.store.write_product_review_decision_record(self.accepted)

    def _train_refreshed(
        self,
        expected_head_sha: str = _ACCEPTED_HEAD,
        result_head_sha: str = _REFRESHED_HEAD,
        merged_base_sha: str = _BASE_COMMIT,
        *,
        base_branch: str = "main",
    ) -> str:
        record = build_merge_train_branch_refresh_record(
            repository=_REPOSITORY,
            base_branch=base_branch,
            pull_request_number=_PULL_REQUEST,
            expected_head_sha=expected_head_sha,
            result_head_sha=result_head_sha,
            merged_base_sha=merged_base_sha,
            requested_at=_REFRESH_REQUESTED_AT,
        )
        self.store.write_merge_train_branch_refresh_record(record)
        return record.record_id

    def _publish(self) -> dict[str, object]:
        OwnerReviewStatusPublisher(
            control_plane_root=Path("/nonexistent"),
            public_origin="https://launchplane.example.invalid",
            github_token=lambda **_: "feedback-token",
            api_request=self.github,
            github_app_token=_app_token,
        ).publish(
            store=require_product_review_store(self.store),
            profile=self.profile,
            pull_request_number=_PULL_REQUEST,
        )
        return self.github.statuses[0]

    def _decisions(self) -> tuple[ProductReviewDecisionRecord, ...]:
        return self.store.list_product_review_decision_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
        )

    def assert_waits_for_the_client(self, status: dict[str, object]) -> None:
        self.assertEqual(
            (status["state"], status["description"]),
            ("pending", "Waiting for @site-owner to review the preview"),
        )
        self.assertEqual(self._decisions(), (self.accepted,))

    def test_acceptance_carries_across_the_trains_base_only_refresh(self) -> None:
        refresh_id = self._train_refreshed()

        status = self._publish()

        self.assertEqual(status["state"], "success")
        self.assertEqual(
            status["description"],
            "Accepted by @site-owner (carried from bd92cee after a base-only refresh)",
        )
        carried, accepted = self._decisions()
        self.assertEqual(accepted, self.accepted)
        self.assertEqual(
            (carried.head_sha, carried.decision, carried.base_branch),
            (_REFRESHED_HEAD, "accepted", "main"),
        )
        assert carried.carried_from is not None
        self.assertEqual(
            (
                carried.carried_from.record_id,
                carried.carried_from.head_sha,
                carried.carried_from.reason,
                carried.carried_from.refresh_record_ids,
            ),
            ("decision-accepted", _ACCEPTED_HEAD, "merge_train_base_refresh", (refresh_id,)),
        )
        # Carried, not re-decided: still the same Client and their delivered feedback.
        self.assertEqual(
            (carried.owner_github_id, carried.feedback_url),
            (_OWNER_GITHUB_ID, self.accepted.feedback_url),
        )

        self._publish()
        self.assertEqual(len(self._decisions()), 2)

    def test_acceptance_carries_across_repeated_train_refreshes(self) -> None:
        first = self._train_refreshed()
        second = self._train_refreshed(_REFRESHED_HEAD, _SECOND_REFRESH_HEAD, _NEWER_BASE_COMMIT)
        self.github.head_sha = _SECOND_REFRESH_HEAD
        self.github.changes = {
            (_NEWER_BASE_COMMIT, _ACCEPTED_HEAD): _change(),
            (_NEWER_BASE_COMMIT, _SECOND_REFRESH_HEAD): _change(),
        }

        self.assertEqual(self._publish()["state"], "success")
        carried = self._decisions()[0]
        assert carried.carried_from is not None
        self.assertEqual(carried.carried_from.refresh_record_ids, (first, second))

    def test_any_head_but_the_trains_own_merge_needs_a_new_decision(self) -> None:
        # The train refreshed from the accepted head, but the pull request's head is
        # another merge of the same parents with the same change, or a new commit.
        self._train_refreshed()
        for head in (_OTHER_MERGE_HEAD, _SECOND_REFRESH_HEAD):
            with self.subTest(head=head):
                self.github.head_sha = head
                self.github.changes[(_BASE_COMMIT, head)] = _change()

                self.assert_waits_for_the_client(self._publish())

    def test_acceptance_carries_when_base_edits_only_move_or_surround_the_change(
        self,
    ) -> None:
        # odoo-tenant-cm-website#92: a docs change on main edited two files the
        # pull request also changes. The lines it adds and removes are the same.
        self._train_refreshed()
        readme = (
            "@@ -79,3 +79,5 @@ Palette\n Colors\n-Old headline\n+New headline\n"
            "+Second line\n Footer"
        )
        moved_readme = (
            "@@ -133,3 +133,5 @@ Brand\n Palette colors\n-Old headline\n+New headline\n"
            "+Second line\n Footer note"
        )
        notes = "@@ -10 +10,2 @@\n Notes\n+Release note\n\\ No newline at end of file"
        accepted = _change(patch=readme) + [
            {"filename": "docs/README.md", "status": "modified", "sha": "d" * 40, "patch": notes}
        ]
        refreshed = _change(patch=moved_readme, blob="b" * 40) + [
            {"filename": "docs/README.md", "status": "modified", "sha": "e" * 40, "patch": notes}
        ]
        self.github.changes[(_BASE_COMMIT, _ACCEPTED_HEAD)] = accepted
        self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = refreshed

        status = self._publish()

        self.assertEqual(status["state"], "success")
        self.assertEqual(self._decisions()[0].head_sha, _REFRESHED_HEAD)

    def test_changed_diff_needs_a_new_decision(self) -> None:
        self._train_refreshed()
        # A conflict resolution or anything else that changes the pull request's change.
        cases: dict[str, list[dict[str, object]]] = {
            "added line": _change(patch=_PATCH + "\n+Extra line"),
            "removed line": _change(patch="@@ -1 +1 @@\n-Older headline\n+New headline"),
            "line order": _change(patch="@@ -1 +1 @@\n+New headline\n-Old headline"),
            "end of file": _change(patch=_PATCH + "\n\\ No newline at end of file"),
            "status": [{**_change()[0], "status": "added"}],
            "file name": [{**_change()[0], "filename": "website/views/about.xml"}],
            "renamed from": [{**_change()[0], "previous_filename": "website/views/old.xml"}],
            "another file": _change() + [{**_change()[0], "filename": "website/b.xml"}],
            "binary": [{"filename": "logo.png", "status": "modified", "sha": "c" * 40}],
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.github.changes[(_BASE_COMMIT, _REFRESHED_HEAD)] = change

                self.assert_waits_for_the_client(self._publish())

    def test_different_base_needs_a_new_decision(self) -> None:
        cases: tuple[tuple[str, str, set[str]], ...] = (
            # Accepted on main, retargeted to release, then the train refreshed on release.
            ("accepted on another base", "release", {_BASE_COMMIT}),
            ("the merged commit is not on the base branch", "main", set()),
        )
        for name, base_branch, on_base in cases:
            with self.subTest(name):
                self.setUp()
                self._train_refreshed(base_branch=base_branch)
                self.github.base_branch = base_branch
                self.github.on_base = on_base

                self.assert_waits_for_the_client(self._publish())

    def test_carried_acceptance_stops_applying_when_the_base_changes(self) -> None:
        self._train_refreshed()
        self.assertEqual(self._publish()["state"], "success")

        # Retargeted without a new commit: the same head is a different change now.
        self.github.base_branch = "release"

        self.assertEqual((self._publish()["state"], len(self._decisions())), ("pending", 2))

    def test_acceptance_without_a_recorded_base_does_not_carry(self) -> None:
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(update={"base_branch": ""})
        )
        self._train_refreshed()

        self.assertEqual(self._publish()["state"], "pending")

    def test_a_decision_keeps_the_base_it_was_first_shown_on(self) -> None:
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(update={"base_branch": ""})
        )
        self.github.head_sha = _ACCEPTED_HEAD

        self.assertEqual(self._publish()["state"], "success")
        (decision,) = self._decisions()
        self.assertEqual(decision.base_branch, "main")

    def test_a_newer_decision_is_never_overridden_by_a_carry(self) -> None:
        self._train_refreshed()
        self.store.write_product_review_decision_record(
            self.accepted.model_copy(
                update={
                    "record_id": "decision-changes",
                    "decision": "changes_requested",
                    "reason": "The headline is wrong.",
                    "decided_at": "2026-10-02T22:30:00.000Z",
                }
            )
        )

        status = self._publish()

        self.assertEqual(status["state"], "pending")
        self.assertEqual(len(self._decisions()), 2)


def _refresh(client: GitHubMergeTrainClient) -> None:
    client.update_pull_request_branch(
        repository=_REPOSITORY,
        pull_request_number=_PULL_REQUEST,
        expected_head_sha=_ACCEPTED_HEAD.upper(),
    )


def _pull_request(head_sha: str) -> dict[str, object]:
    return {"head": {"sha": head_sha}}


def _commit(*parents: str) -> dict[str, object]:
    return {"parents": [{"sha": parent} for parent in parents]}


class MergeTrainBranchRefreshRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary_directory.name))
        self.waits: list[float] = []

    def _client(self, transport: RecordingMergeTrainGitHubTransport) -> GitHubMergeTrainClient:
        return GitHubMergeTrainClient(
            transport=transport,
            branch_refresh_recorder=merge_train_branch_refresh_recorder(
                store=self.store, base_branch="main", trace_id="trace-1"
            ),
            wait=self.waits.append,
        )

    def _refreshes(self) -> tuple[MergeTrainBranchRefreshRecord, ...]:
        return self.store.list_merge_train_branch_refresh_records(
            repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
        )

    def test_train_records_the_merge_commit_its_refresh_made(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                {},
                # GitHub has not made the merge commit yet, then it has.
                _pull_request(_ACCEPTED_HEAD),
                _pull_request(_REFRESHED_HEAD),
                _commit(_ACCEPTED_HEAD, _BASE_COMMIT),
            )
        )

        _refresh(self._client(transport))

        (record,) = self._refreshes()
        self.assertEqual(
            (
                record.expected_head_sha,
                record.result_head_sha,
                record.merged_base_sha,
                record.base_branch,
                record.trace_id,
            ),
            (_ACCEPTED_HEAD, _REFRESHED_HEAD, _BASE_COMMIT, "main", "trace-1"),
        )
        self.assertEqual(transport.requests[0].method, "PUT")
        self.assertEqual(len(self.waits), 1)

    def test_a_head_that_is_not_the_refresh_merge_is_not_recorded(self) -> None:
        cases = {
            "someone pushed a commit": _commit(_ACCEPTED_HEAD),
            "a merge from another head": _commit(_OTHER_MERGE_HEAD, _BASE_COMMIT),
        }
        for name, commit in cases.items():
            with self.subTest(name):
                transport = RecordingMergeTrainGitHubTransport(
                    responses=({}, _pull_request(_REFRESHED_HEAD), commit)
                )

                _refresh(self._client(transport))

                self.assertEqual(self._refreshes(), ())

    def test_a_refresh_never_seen_is_not_recorded(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({},) + (_pull_request(_ACCEPTED_HEAD),) * 20
        )

        with self.assertLogs("control_plane.merge_train_github", "INFO"):
            _refresh(self._client(transport))

        self.assertEqual(self._refreshes(), ())

    def test_database_keeps_each_pull_requests_refreshes(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        records = [
            build_merge_train_branch_refresh_record(
                repository=_REPOSITORY.upper(),
                base_branch="main",
                pull_request_number=number,
                expected_head_sha=_ACCEPTED_HEAD,
                result_head_sha=_REFRESHED_HEAD,
                merged_base_sha=_BASE_COMMIT,
                requested_at=_REFRESH_REQUESTED_AT,
            )
            for number in (_PULL_REQUEST, _PULL_REQUEST + 1)
        ]
        for record in records:
            store.write_merge_train_branch_refresh_record(record)

        self.assertEqual(
            store.list_merge_train_branch_refresh_records(
                repository=_REPOSITORY, pull_request_number=_PULL_REQUEST
            ),
            (records[0],),
        )

    def test_refresh_is_not_recorded_when_the_provider_refuses_it(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(RuntimeError("expected head moved"),)
        )

        with self.assertRaises(RuntimeError):
            _refresh(self._client(transport))

        self.assertEqual(self._refreshes(), ())

    def test_a_refresh_that_cannot_be_recorded_still_happens(self) -> None:
        def unavailable(**_: object) -> None:
            raise OSError("records are unavailable")

        transport = RecordingMergeTrainGitHubTransport(
            responses=({}, _pull_request(_REFRESHED_HEAD), _commit(_ACCEPTED_HEAD, _BASE_COMMIT))
        )

        with self.assertLogs("control_plane.merge_train_github", "WARNING"):
            _refresh(
                GitHubMergeTrainClient(transport=transport, branch_refresh_recorder=unavailable)
            )

        self.assertEqual(transport.requests[0].method, "PUT")


if __name__ == "__main__":
    unittest.main()
