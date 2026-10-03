from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
    build_merge_train_branch_refresh_record,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_merge_train_dependency_updates import INDIRECT_PATCH, MAJOR
from tests.test_merge_train_github import (
    _check_run,
    _combined_status,
    _github_branch,
    _github_commit,
    _github_pull_request,
    _label_events,
)

REPOSITORY = "cbusillo/sellyouroutboard"
BOT_ID = 49699333
OLD_HEAD = "1" * 40
REFRESH_HEAD = "2" * 40
BASE = "3" * 40


def _refresh_record(**updates: object) -> MergeTrainBranchRefreshRecord:
    record = build_merge_train_branch_refresh_record(
        repository=REPOSITORY,
        base_branch="main",
        pull_request_number=16,
        expected_head_sha=OLD_HEAD,
        result_head_sha=REFRESH_HEAD,
        merged_base_sha=BASE,
        requested_at=datetime.now(timezone.utc),
    )
    return MergeTrainBranchRefreshRecord.model_validate(record.model_dump() | updates)


def _refresh_commit(**updates: object) -> dict[str, object]:
    return _github_commit(1234, "Merge main into dependabot branch", sha=REFRESH_HEAD) | {
        "parents": [{"sha": OLD_HEAD}, {"sha": BASE}],
        **updates,
    }


class DependencyRefreshTests(unittest.TestCase):
    def _classify(
        self,
        *,
        commits: list[dict[str, object]],
        records: tuple[MergeTrainBranchRefreshRecord, ...] = (),
        ancestry: tuple[object, ...] = (),
        timeline: list[dict[str, object]] | None = None,
        head: str = REFRESH_HEAD,
        use_store: bool = True,
    ) -> str | None:
        pull_request = _github_pull_request(16, author_association="CONTRIBUTOR", head_sha=head)
        pull_request["user"] = {"id": BOT_ID, "login": "dependabot[bot]", "type": "Bot"}
        responses: tuple[object, ...] = (
            _github_branch(),
            [pull_request],
            pull_request,
            MergeTrainGitHubError("permission not found", status_code=404),
            _label_events(),
        )

        # Refused classifications exit before reading the timeline. Provide responses
        # by path rather than their position so a refusal can still read CI evidence.
        class Transport(RecordingMergeTrainGitHubTransport):
            def request(
                self, *, method: str, path: str, body: dict[str, object] | None = None
            ) -> object:
                if "/timeline?" in path:
                    return [] if timeline is None else timeline
                if "/commits?" in path:
                    return commits
                if "/compare/" in path:
                    result = next(comparisons)
                    if isinstance(result, Exception):
                        raise result
                    return result
                if "/status" in path:
                    return _combined_status()
                if "/check-runs" in path:
                    return {"check_runs": [_check_run("completed", "success")]}
                return super().request(method=method, path=path, body=body)

        comparisons = iter(ancestry)
        transport = Transport(responses=responses)
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            for record in records:
                store.write_merge_train_branch_refresh_record(record)
            snapshot = GitHubMergeTrainClient(
                transport=transport,
                branch_refresh_store=store if use_store else None,
            ).read_merge_train_snapshot(repository=REPOSITORY, base_branch="main")
        return snapshot.pull_requests[0].dependency_update_class

    def test_recorded_refresh_preserves_patch_admission(self) -> None:
        self.assertEqual(
            self._classify(
                commits=[_github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD), _refresh_commit()],
                records=(_refresh_record(),),
                ancestry=({"status": "ahead"},),
            ),
            "patch_or_minor",
        )

    def test_multiple_recorded_refreshes_preserve_admission(self) -> None:
        newest = "4" * 40
        self.assertEqual(
            self._classify(
                commits=[
                    _github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD),
                    _refresh_commit(),
                    _refresh_commit(sha=newest, parents=[{"sha": REFRESH_HEAD}, {"sha": BASE}]),
                ],
                records=(
                    _refresh_record(),
                    _refresh_record(expected_head_sha=REFRESH_HEAD, result_head_sha=newest),
                ),
                ancestry=({"status": "identical"}, {"status": "ahead"}),
                head=newest,
            ),
            "patch_or_minor",
        )

    def test_refresh_does_not_admit_major_updates_or_other_authors(self) -> None:
        for message, author in ((MAJOR, BOT_ID), (INDIRECT_PATCH, 789)):
            with self.subTest(message=message, author=author):
                self.assertEqual(
                    self._classify(
                        commits=[_github_commit(author, message, sha=OLD_HEAD), _refresh_commit()],
                        records=(_refresh_record(),),
                        ancestry=({"status": "ahead"},),
                    ),
                    "needs_review",
                )

    def test_refresh_requires_exact_record_and_parents(self) -> None:
        for changes in (
            {"repository": "other/repo"},
            {"pull_request_number": 17},
            {"base_branch": "release"},
            {"result_head_sha": "5" * 40},
            {"expected_head_sha": "5" * 40},
            {"merged_base_sha": "5" * 40},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self._classify(
                        commits=[
                            _github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD),
                            _refresh_commit(),
                        ],
                        records=(_refresh_record(**changes),),
                    ),
                    "needs_review",
                )
        for parents in ([], [{"sha": OLD_HEAD}], [{"sha": BASE}, {"sha": OLD_HEAD}]):
            with self.subTest(parents=parents):
                self.assertEqual(
                    self._classify(
                        commits=[
                            _github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD),
                            _refresh_commit(parents=parents),
                        ],
                        records=(_refresh_record(),),
                    ),
                    "needs_review",
                )

    def test_missing_record_or_store_withholds_admission(self) -> None:
        for use_store in (False, True):
            with self.subTest(use_store=use_store):
                self.assertEqual(
                    self._classify(
                        commits=[
                            _github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD),
                            _refresh_commit(),
                        ],
                        use_store=use_store,
                    ),
                    "needs_review",
                )

    def test_unverified_refresh_and_foreign_force_push_withhold_admission(self) -> None:
        cases: tuple[tuple[dict[str, object], list[dict[str, object]]], ...] = (
            (
                _refresh_commit(commit={"message": "refresh", "verification": {"verified": False}}),
                [],
            ),
            (_refresh_commit(committer={"id": 1234}), []),
            (_refresh_commit(), [{"event": "head_ref_force_pushed", "actor": {"id": 1234}}]),
        )
        for refresh, timeline in cases:
            with self.subTest(refresh=refresh, timeline=timeline):
                self.assertEqual(
                    self._classify(
                        commits=[_github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD), refresh],
                        records=(_refresh_record(),),
                        ancestry=({"status": "ahead"},),
                        timeline=timeline,
                    ),
                    "needs_review",
                )

    def test_base_ancestry_must_be_proven(self) -> None:
        for comparison in (
            {"status": "diverged"},
            {},
            MergeTrainGitHubError("refused", status_code=403),
        ):
            with self.subTest(comparison=comparison):
                self.assertEqual(
                    self._classify(
                        commits=[
                            _github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD),
                            _refresh_commit(),
                        ],
                        records=(_refresh_record(),),
                        ancestry=(comparison,),
                    ),
                    "needs_review",
                )

    def test_old_head_and_matching_snapshot_are_required(self) -> None:
        for commits, head in (
            ([_refresh_commit()], REFRESH_HEAD),
            ([_github_commit(BOT_ID, INDIRECT_PATCH, sha=OLD_HEAD), _refresh_commit()], "5" * 40),
        ):
            with self.subTest(commits=commits, head=head):
                self.assertEqual(
                    self._classify(commits=commits, records=(_refresh_record(),), head=head),
                    "needs_review",
                )
