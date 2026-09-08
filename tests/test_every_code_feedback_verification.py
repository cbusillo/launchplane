from __future__ import annotations

from copy import deepcopy
import unittest

from control_plane.every_code_feedback_verification import verify_every_code_feedback_revision


def _fixture(kind: str = "issue_comment") -> dict[str, object]:
    repository = {"id": 42, "full_name": "example/repo", "owner": {"id": 7}}
    pull_request = {
        "number": 11,
        "node_id": "PR_example",
        "state": "open",
        "url": "https://api.github.com/repos/example/repo/pulls/11",
        "issue_url": "https://api.github.com/repos/example/repo/issues/11",
        "base": {"repo": repository},
    }
    author = {"id": 17, "login": "example-author", "type": "User"}
    feedback = {
        "id": 81,
        "node_id": "COMMENT_example",
        "user": author,
        "body": "Please add the missing test.",
        "updated_at": "2026-09-08T00:00:00Z",
        "submitted_at": "2026-09-08T00:00:00Z",
        "issue_url": pull_request["issue_url"],
        "pull_request_url": pull_request["url"],
    }
    delivery: dict[str, object] = {
        "action": "submitted" if kind == "pull_request_review" else "created",
        "repository": deepcopy(repository),
        "sender": deepcopy(author),
        "review" if kind == "pull_request_review" else "comment": deepcopy(feedback),
    }
    if kind == "issue_comment":
        delivery["issue"] = {
            "number": 11,
            "node_id": "PR_example",
            "pull_request": {"url": pull_request["url"]},
        }
    else:
        delivery["pull_request"] = deepcopy(pull_request)
    return {
        "event_name": kind,
        "delivery": delivery,
        "canonical_repository": repository,
        "canonical_pull_request": pull_request,
        "canonical_feedback": feedback,
        "observed_at": "2026-09-08T00:01:00Z",
    }


class EveryCodeFeedbackVerificationTests(unittest.TestCase):
    @staticmethod
    def verify(fixture: dict[str, object]) -> object:
        # Fixtures intentionally exercise the untyped JSON provider boundary.
        return verify_every_code_feedback_revision(**fixture)  # type: ignore[arg-type]

    def test_all_supported_feedback_kinds_bind_same_immutable_actor_and_repository(self) -> None:
        for kind in ("issue_comment", "pull_request_review", "pull_request_review_comment"):
            with self.subTest(kind=kind):
                fixture = _fixture(kind)
                revision = verify_every_code_feedback_revision(**fixture)  # type: ignore[arg-type]
                self.assertEqual(revision.actor_github_id, 17)
                self.assertEqual(revision.repository_id, 42)
                self.assertEqual(revision.provider_updated_at, "2026-09-08T00:00:00.000000Z")

    def test_changed_body_or_provider_identity_cannot_be_authorized_by_signed_delivery(
        self,
    ) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("body", "Run a different task"),
            ("id", 82),
            ("id", True),
            ("node_id", "COMMENT_different"),
            ("updated_at", "2026-09-08T00:00:01Z"),
            ("issue_url", "https://api.github.com/repos/example/repo/issues/12"),
            ("user", {"id": 18, "login": "other", "type": "User"}),
            ("user", {"id": 17, "login": "automation", "type": "Bot"}),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                fixture = _fixture()
                canonical = fixture["canonical_feedback"]
                assert isinstance(canonical, dict)
                canonical[key] = value
                with self.assertRaises(ValueError):
                    self.verify(fixture)

    def test_closed_or_cross_repository_pull_request_is_rejected(self) -> None:
        for field, value in (
            ("state", "closed"),
            ("number", 12),
            ("node_id", "PR_other"),
            ("base", {"repo": {"id": 43, "owner": {"id": 7}}}),
        ):
            with self.subTest(field=field):
                fixture = _fixture()
                pr = fixture["canonical_pull_request"]
                assert isinstance(pr, dict)
                pr[field] = value
                with self.assertRaises(ValueError):
                    self.verify(fixture)

    def test_sender_cannot_attribute_another_authors_feedback_to_themselves(self) -> None:
        fixture = _fixture()
        delivery = fixture["delivery"]
        assert isinstance(delivery, dict)
        delivery["sender"] = {"id": 18, "login": "other", "type": "User"}
        with self.assertRaises(ValueError):
            self.verify(fixture)

    def test_missing_canonical_reads_fail_closed(self) -> None:
        for name in ("canonical_repository", "canonical_pull_request", "canonical_feedback"):
            with self.subTest(name=name):
                fixture = _fixture()
                fixture[name] = {}
                with self.assertRaises(ValueError):
                    self.verify(fixture)

    def test_old_future_and_naive_provider_times_are_rejected(self) -> None:
        for timestamp in (
            "2026-09-06T00:00:00Z",
            "2026-09-08T00:06:01Z",
            "2026-09-08T00:00:00",
        ):
            with self.subTest(timestamp=timestamp):
                fixture = _fixture()
                delivery = fixture["delivery"]
                canonical = fixture["canonical_feedback"]
                assert isinstance(delivery, dict) and isinstance(canonical, dict)
                delivery["comment"]["updated_at"] = timestamp
                canonical["updated_at"] = timestamp
                with self.assertRaises(ValueError):
                    self.verify(fixture)

    def test_review_uses_submitted_time_without_inventing_update_time(self) -> None:
        fixture = _fixture("pull_request_review")
        delivery = fixture["delivery"]
        canonical = fixture["canonical_feedback"]
        assert isinstance(delivery, dict) and isinstance(canonical, dict)
        del canonical["updated_at"]
        del delivery["review"]["updated_at"]
        self.verify(fixture)

    def test_edited_events_and_exact_provider_time_limits(self) -> None:
        for kind in ("issue_comment", "pull_request_review", "pull_request_review_comment"):
            fixture = _fixture(kind)
            delivery = fixture["delivery"]
            assert isinstance(delivery, dict)
            delivery["action"] = "edited"
            self.verify(fixture)
        for timestamp in ("2026-09-07T00:01:00Z", "2026-09-08T00:06:00Z"):
            fixture = _fixture()
            delivery = fixture["delivery"]
            canonical = fixture["canonical_feedback"]
            assert isinstance(delivery, dict) and isinstance(canonical, dict)
            delivery["comment"]["updated_at"] = timestamp
            canonical["updated_at"] = timestamp
            self.verify(fixture)

    def test_malformed_delivery_and_canonical_owner_fail_closed(self) -> None:
        cases: tuple[tuple[tuple[str, ...], object], ...] = (
            (("delivery", "repository", "id"), True),
            (("delivery", "repository", "owner", "id"), "7"),
            (("delivery", "issue", "number"), False),
            (("delivery", "issue", "pull_request", "url"), "https://example.test/other"),
            (("delivery", "sender", "type"), "Bot"),
            (("delivery", "comment", "user", "id"), None),
            (("delivery", "action"), []),
            (("canonical_pull_request", "base", "repo", "owner", "id"), 8),
        )
        for path, value in cases:
            with self.subTest(path=path, value=value):
                fixture = _fixture()
                target = fixture
                for key in path[:-1]:
                    child = target[key]
                    assert isinstance(child, dict)
                    target = child
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    self.verify(fixture)


if __name__ == "__main__":
    unittest.main()
