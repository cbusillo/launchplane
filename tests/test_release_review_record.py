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

    def test_old_issue_history_does_not_block_a_new_release(self) -> None:
        old = [
            {"number": number + 1, "body": "Old issue", "created_at": "2020-01-01T00:00:00Z"}
            for number in range(100)
        ]
        with patch(
            "control_plane.release_review_record.github_api_request",
            side_effect=[old, {"number": 1201}],
        ) as api:
            url = publish_release_decision(
                control_plane_root=self.root, profile=profile(), decision=self.decision
            )
        self.assertEqual(url, "https://github.com/example/site/issues/1201")
        self.assertEqual(api.call_count, 2)

    def test_marker_without_complete_record_is_not_a_successful_write(self) -> None:
        forged = (
            release_decision_issue_body(self.decision).splitlines()[0] + "\nDifferent checklist"
        )
        with patch(
            "control_plane.release_review_record.github_api_request",
            side_effect=[[{"number": 99, "body": forged}], {"number": 100}],
        ):
            url = publish_release_decision(
                control_plane_root=self.root, profile=profile(), decision=self.decision
            )
        self.assertEqual(url, "https://github.com/example/site/issues/100")

    def test_large_record_resumes_all_comments_after_a_lost_response(self) -> None:
        checklist = self.decision.checklist
        item = checklist.items[0].model_copy(
            update={"owner_test_notes": "Check the phone layout 🛠.\n" * 4000}
        )
        record = self.decision.model_copy(
            update={"checklist": checklist.model_copy(update={"items": (item,)})}
        )
        issues: list[dict[str, object]] = []
        comments: list[dict[str, object]] = []
        lost_response = False

        def api(
            *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
        ) -> object:
            nonlocal lost_response
            del token
            if method == "GET":
                return comments.copy() if "/comments?" in path else issues.copy()
            assert body is not None
            if path.endswith("/issues"):
                created = {"number": 99, **body}
                issues.append(created)
                return created
            created = {"id": len(comments) + 1, **body}
            comments.append(created)
            if len(comments) == 2 and not lost_response:
                lost_response = True
                raise ValueError("Provider accepted the comment but its response was lost")
            return created

        with patch("control_plane.release_review_record.github_api_request", side_effect=api):
            with self.assertRaises(ValueError):
                publish_release_decision(
                    control_plane_root=self.root, profile=profile(), decision=record
                )
            url = publish_release_decision(
                control_plane_root=self.root, profile=profile(), decision=record
            )
        self.assertEqual(url, "https://github.com/example/site/issues/99")
        self.assertEqual(len(issues), 1)
        comment_bodies = []
        for comment in comments:
            body = comment["body"]
            assert isinstance(body, str)
            comment_bodies.append(body)
        self.assertEqual(len(comment_bodies), len(set(comment_bodies)))
        self.assertTrue(all(len(body.encode()) <= 60000 for body in comment_bodies))
        recovered = "".join(
            "\n".join(body.split("\n\n", 2)[2].split("\n")[1:-1]) for body in comment_bodies
        )
        self.assertEqual(recovered, release_decision_issue_body(record))

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
