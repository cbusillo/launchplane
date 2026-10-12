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

    def test_historical_engineering_notes_still_require_reason_and_respect_label(self) -> None:
        for notes in (
            "Nothing for the owner to test: CI only.",
            "Nothing for the owner to test. CI only.",
        ):
            with self.subTest(notes=notes):
                self.assertFalse(client_test_notes(notes).review_required)
                self.assertFalse(client_test_notes(notes).error)
                self.assertTrue(client_test_notes(notes, review_override=True).error)
        self.assertTrue(client_test_notes("Nothing for the owner to test").error)

    def test_open_pr_admission_requires_a_reason_in_the_supported_format(self) -> None:
        for notes in (
            "Nothing for the Client to test",
            "Nothing for the owner to test",
            "Nothing for the Client to test.",
            "Nothing for the Client to test. Automated dependency update.",
            "nothing for the Client to test: CI only.",
            "Nothing for the owner to test - CI only.",
        ):
            with self.subTest(notes=notes):
                self.assertTrue(client_test_notes(notes).error)
                self.assertTrue(client_test_notes(notes, review_override=True).error)

    def test_placeholder_punctuation_is_not_a_client_instruction(self) -> None:
        for notes in ("N/A.", "None.", "TBD - later", "<!-- old template -->"):
            with self.subTest(notes=notes):
                self.assertTrue(client_test_notes(notes).error)

    def test_formatted_declaration_is_refused_instead_of_requesting_review(self) -> None:
        for notes in (
            "- Nothing for the Client to test: CI only.",
            "**Nothing for the Client to test:** CI only.",
        ):
            with self.subTest(notes=notes):
                self.assertTrue(client_test_notes(notes).error)
        intent = client_test_notes(
            "Confirm the help text explains Nothing for the Client to test correctly."
        )
        self.assertTrue(intent.review_required)
        self.assertFalse(intent.error)

    def test_workflow_feedback_reads_notes_and_skips_only_bound_train_heads(self) -> None:
        from contextlib import nullcontext
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from control_plane.client_test_notes import ClientTestNotesRequired
        from control_plane.workflows.preview_pr_feedback import pull_request_client_review

        head: dict[str, object] = {
            "ref": "launchplane/train/example/site/main/batch",
            "sha": "a" * 40,
            "repo": {"full_name": "example/site"},
        }
        payload: dict[str, object] = {
            "body": "## Client test notes\nTry the price list.",
            "head": head,
            "base": {"ref": "main"},
            "labels": [],
        }
        candidate = SimpleNamespace(
            candidate_ref="refs/heads/launchplane/train/example/site/main/batch",
            candidate_sha="a" * 40,
            base_branch="main",
        )
        store = SimpleNamespace(
            list_merge_train_batch_candidate_records=lambda **_: (
                SimpleNamespace(candidate=candidate),
            )
        )
        with (
            patch(
                "control_plane.workflows.preview_pr_feedback.launchplane_github_token",
                return_value=nullcontext("test-token"),
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                return_value=payload,
            ),
        ):

            def read(
                record_store: object = None,
                *,
                client_set: bool = True,
                director_standing: bool = False,
            ) -> bool:
                return pull_request_client_review(
                    control_plane_root=Path("."),
                    context="test",
                    anchor_pr_url="https://github.com/example/site/pull/42",
                    label="owner-review",
                    record_store=record_store,
                    client_set=client_set,
                    director_standing=director_standing,
                )

            self.assertTrue(read())
            self.assertFalse(read(director_standing=True))
            self.assertFalse(read(store))
            payload["body"] = "Synthetic train candidate."
            self.assertFalse(read(store))
            with self.assertRaises(ClientTestNotesRequired):
                read()
            self.assertFalse(read(director_standing=True))
            payload["body"] = "## Client test notes\nTry the price list."
            payload["base"] = {"ref": "other-base"}
            self.assertTrue(read(store))
            payload["base"] = {"ref": "main"}
            payload["labels"] = [{"name": "owner-review"}]
            self.assertTrue(read(store))
            payload["labels"] = []
            payload["head"] = head | {"sha": "b" * 40}
            self.assertTrue(read(store))
            payload["body"] = "## Client test notes\nNothing for the Client to test: CI only."
            self.assertFalse(read())
            payload["labels"] = [{"name": "owner-review"}]
            with self.assertRaises(ClientTestNotesRequired):
                read()
            self.assertTrue(read(client_set=False))
            payload["labels"] = []
            self.assertFalse(read(client_set=False))


class BatchNotesTests(unittest.TestCase):
    def test_engineering_batch_reasons_are_validated_per_member(self) -> None:
        notes = "### #1 CI\n\nNothing for the Client to test: Faster lint.\n\n### #2 Tests\n\nNothing for the Client to test: Added coverage."
        self.assertFalse(client_test_notes(notes).review_required)
        self.assertFalse(client_test_notes(notes).error)
        self.assertTrue(
            client_test_notes("Try the changed price list.\n\n" + notes).review_required
        )
        self.assertFalse(client_test_notes("<!-- generated batch -->\n" + notes).review_required)
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
            base_branch="main",
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
        self.assertTrue(
            recorded_train_candidate(
                store=store, repository="example/site", head=head, base_branch="main"
            )
        )
        self.assertFalse(
            recorded_train_candidate(
                store=store,
                repository="example/site",
                head=head | {"sha": "b" * 40},
                base_branch="main",
            )
        )
        self.assertFalse(
            recorded_train_candidate(
                store=store,
                repository="example/site",
                head=head | {"repo": {"full_name": "outside/site"}},
                base_branch="main",
            )
        )
