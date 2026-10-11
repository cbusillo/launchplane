from types import SimpleNamespace
from typing import Any, cast
import unittest

from control_plane.merge_train_github import (
    GitHubMergeTrainSnapshotReader,
    RecordingMergeTrainGitHubTransport,
)
from tests.test_merge_train_github import _github_pull_request


class DependabotClientReviewTests(unittest.TestCase):
    def test_client_repository_dependency_update_needs_no_notes_or_review(self) -> None:
        pull = _github_pull_request(16)
        pull["body"] = "Bumps a dependency."
        pull["labels"] = []
        pull["user"] = {"id": 49699333, "login": "dependabot[bot]", "type": "Bot"}
        profile = SimpleNamespace(
            is_active=True,
            repository="cbusillo/sellyouroutboard",
            owner=SimpleNamespace(is_set=True, review_label="owner-review"),
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"files": [{"filename": "package.json", "status": "modified"}]},)
        )
        reader = GitHubMergeTrainSnapshotReader(
            transport=transport,
            branch_refresh_store=cast(
                Any, SimpleNamespace(list_product_profile_records=lambda: (profile,))
            ),
        )
        snapshot = reader._pull_request_snapshot(
            repository_path=profile.repository, pull_request=pull, enrich=False
        )
        self.assertFalse(snapshot.client_test_notes_error)
        self.assertFalse(snapshot.owner_review_required)

    def test_only_complete_dependency_changes_qualify(self) -> None:
        from control_plane.dependency_update_review import pure_dependabot_update

        pull = _github_pull_request(16)
        pull["labels"] = []
        pull["user"] = {"id": 49699333, "login": "dependabot[bot]", "type": "Bot"}
        good = [
            {"filename": "frontend/package.json", "status": "modified"},
            {"filename": "frontend/pnpm-lock.yaml", "status": "modified"},
        ]
        for changes, expected in (
            (good, True),
            ([{"filename": "uv.lock", "status": "modified"}], True),
            ([{"filename": "requirements.txt", "status": "modified"}], True),
            (good + [{"filename": "app.py", "status": "modified"}], False),
            ([{"filename": ".github/workflows/ci.yml", "status": "modified"}], False),
            ([{"filename": "Dockerfile", "status": "modified"}], False),
            (
                [{"filename": "package.json", "status": "renamed", "previous_filename": "app.py"}],
                False,
            ),
            ([], False),
            (good * 150, False),
            ([{"filename": "package.json"}], False),
        ):
            with self.subTest(changes=changes[:2], count=len(changes)):
                self.assertEqual(
                    pure_dependabot_update(pull, read=lambda _: {"files": changes}), expected
                )
        for update in (
            {"user": {"login": "agent[bot]", "type": "Bot"}},
            {"user": {"login": "person", "type": "User"}},
            {"labels": [{"name": "owner-review"}]},
            {"head": {"sha": "new", "repo": {"full_name": "someone/fork"}}},
            {"changed_files": 3},
        ):
            with self.subTest(update=update):
                self.assertFalse(
                    pure_dependabot_update(pull | update, read=lambda _: {"files": good})
                )

        def refused(_: str) -> object:
            raise ValueError("read refused")

        self.assertFalse(pure_dependabot_update(pull, read=refused))
        self.assertFalse(pure_dependabot_update(pull, read=lambda _: {"message": "unavailable"}))

    def test_configured_review_override_still_requires_notes(self) -> None:
        from control_plane.client_test_notes import change_client_review

        intent = change_client_review(
            body="",
            labels=["custom-review"],
            review_label="custom-review",
            pure_dependency_update=True,
        )
        self.assertTrue(intent.error)
        self.assertTrue(intent.review_required)
