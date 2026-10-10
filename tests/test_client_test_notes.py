"""Explicit engineering notes and real test instructions drive Client review."""

import unittest

from control_plane.client_test_notes import change_client_review, client_test_notes


class ClientTestNotesTests(unittest.TestCase):
    def test_real_notes_request_review_without_a_label(self) -> None:
        intent = change_client_review(
            body="## Client test notes\nTry the new price list.",
            labels=(),
            review_label="owner-review",
        )
        self.assertTrue(intent.review_required)
        self.assertFalse(intent.error)

    def test_explicit_engineering_notes_require_a_reason_and_respect_override(self) -> None:
        intent = client_test_notes("Nothing for the Client to test: CI plumbing only.")
        self.assertFalse(intent.review_required)
        self.assertFalse(intent.error)
        for notes in (
            "",
            "<!-- template -->",
            "TBD",
            "Nothing for the Client to test",
            "Nothing for the Client to test: \n",
            "Nothing for the Client to test. CI only.",
        ):
            with self.subTest(notes=notes):
                refused = client_test_notes(notes)
                self.assertTrue(refused.error)
                self.assertTrue(refused.review_required)
        self.assertTrue(
            client_test_notes(
                "Nothing for the Client to test: CI only.", review_override=True
            ).error
        )

    def test_legacy_heading_is_still_read_and_fenced_example_is_not_notes(self) -> None:
        self.assertTrue(
            change_client_review(
                body="## Owner test notes\nTry checkout.", labels=(), review_label="owner-review"
            ).review_required
        )
        self.assertTrue(
            change_client_review(
                body="```md\n## Client test notes\nTry checkout.\n```",
                labels=(),
                review_label="owner-review",
            ).error
        )


class BatchNotesTests(unittest.TestCase):
    def test_engineering_batch_reasons_are_validated_per_member(self) -> None:
        notes = "### #1 CI\n\nNothing for the Client to test: Faster lint.\n\n### #2 Tests\n\nNothing for the Client to test: Added coverage."
        self.assertFalse(client_test_notes(notes).review_required)
        self.assertFalse(client_test_notes(notes).error)
        self.assertTrue(client_test_notes(notes.replace("Added coverage.", "")).error)
        self.assertTrue(client_test_notes(notes, review_override=True).error)
        self.assertTrue(
            client_test_notes(
                notes.replace(
                    "Nothing for the Client to test: Added coverage.", "Try the new price list."
                )
            ).review_required
        )

    def test_candidate_exemption_requires_recorded_head_and_repository(self) -> None:
        from types import SimpleNamespace
        from control_plane.client_release_repositories import recorded_train_candidate

        candidate = SimpleNamespace(
            candidate_ref="refs/heads/launchplane/train/example/site/main/batch",
            candidate_sha="a" * 40,
        )
        store = SimpleNamespace(
            list_merge_train_batch_candidate_records=lambda **_: (
                SimpleNamespace(candidate=candidate),
            )
        )
        head = {
            "ref": candidate.candidate_ref.removeprefix("refs/heads/"),
            "sha": candidate.candidate_sha,
            "repo": {"full_name": "example/site"},
        }
        self.assertTrue(recorded_train_candidate(store=store, repository="example/site", head=head))
        self.assertFalse(
            recorded_train_candidate(
                store=store, repository="example/site", head=head | {"sha": "b" * 40}
            )
        )
        self.assertFalse(
            recorded_train_candidate(
                store=store,
                repository="example/site",
                head=head | {"repo": {"full_name": "outside/site"}},
            )
        )
