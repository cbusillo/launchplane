from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.release_review_record import (
    publish_release_decision,
    release_decision_issue_body,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_release_review import decision, profile, seed


class ReleaseReviewRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        store = FilesystemRecordStore(self.root)
        seed(store)
        self.decision = decision(store)
        token = patch(
            "control_plane.release_review_record.resolve_launchplane_github_token",
            return_value="test-token",
        )
        token.start()
        self.addCleanup(token.stop)

    def test_publishes_full_checklist_and_decision_to_tenant_repository(self) -> None:
        with patch(
            "control_plane.release_review_record.github_api_request",
            side_effect=[[], {"number": 99}],
        ) as api:
            url = publish_release_decision(
                control_plane_root=self.root, profile=profile(), decision=self.decision
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        write = api.call_args.kwargs
        self.assertEqual(write["method"], "POST")
        self.assertEqual(write["path"], "/repos/example/site/issues")
        body = write["body"]["body"]
        self.assertIn("Check the repair prices.", body)
        self.assertIn(self.decision.checklist_digest, body)
        self.assertIn(self.decision.checklist.production.source_commit, body)
        self.assertIn(self.decision.checklist.candidate.source_commit, body)
        self.assertIn("site-owner", body)

    def test_recovers_successful_issue_write_without_a_duplicate(self) -> None:
        with patch(
            "control_plane.release_review_record.github_api_request",
            return_value=[{"number": 99, "body": release_decision_issue_body(self.decision)}],
        ) as api:
            url = publish_release_decision(
                control_plane_root=self.root, profile=profile(), decision=self.decision
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        self.assertEqual(api.call_count, 1)
        self.assertNotIn("method", api.call_args.kwargs)

    def test_incomplete_lookup_or_create_never_claims_a_release_record(self) -> None:
        cases: tuple[list[object], ...] = (
            [{}],
            [[], {}],
            [[{"number": "bad", "body": release_decision_issue_body(self.decision)}]],
        )
        for responses in cases:
            with (
                self.subTest(responses=responses),
                patch(
                    "control_plane.release_review_record.github_api_request", side_effect=responses
                ),
                self.assertRaises(ValueError),
            ):
                publish_release_decision(
                    control_plane_root=self.root, profile=profile(), decision=self.decision
                )

    def test_notes_and_reasons_cannot_escape_literal_blocks_or_ping_people(self) -> None:
        record = self.decision.model_copy(update={"reason": "```\n@someone please read\n```"})
        body = release_decision_issue_body(record)
        self.assertIn("````\n```\n@\u200bsomeone please read\n```\n````", body)
        self.assertNotIn("@someone", body)
