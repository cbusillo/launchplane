"""Explicit engineering notes and real test instructions drive Client review."""

import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from github_plan_status.client_test_notes import ENGINEERING_RELEASE_TESTS

from control_plane.client_test_notes import change_client_review, client_test_notes


class ClientTestNotesTests(unittest.TestCase):
    def test_review_notes_reader_validates_the_whole_body_and_review_label(self) -> None:
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.release_review import read_pull_request_notes
        from tests.support.profiles import product_profile_payload

        profile = LaunchplaneProductProfileRecord.model_validate(product_profile_payload("site"))
        for body, labels, blocked in (
            (
                "## Client test notes\nNothing for the Client to test: CI only.",
                [profile.owner.review_label],
                True,
            ),
            (
                "## Client test notes\nCheck checkout.\n## Owner test notes\nNothing for Justin to test.",
                [],
                True,
            ),
            ("## Client test notes\nCheck checkout.", [], False),
        ):
            with (
                self.subTest(body=body, labels=labels),
                patch(
                    "control_plane.release_review.launchplane_github_token",
                    return_value=nullcontext("fixture"),
                ),
                patch(
                    "control_plane.release_review._read_release_github",
                    return_value={
                        "title": "Checkout",
                        "body": body,
                        "labels": [{"name": label} for label in labels],
                    },
                ),
            ):
                notes = read_pull_request_notes(
                    control_plane_root=Path("unused"),
                    profile=profile,
                    pull_request_number=42,
                    trace_id="fixture",
                )
                self.assertEqual(bool(notes.client_test_notes_error), blocked)
                self.assertEqual(
                    notes.client_test_notes_error,
                    change_client_review(
                        body=body, labels=labels, review_label=profile.owner.review_label
                    ).error,
                )

    def test_questions_keep_the_review_notification_without_permitting_acceptance(self) -> None:
        from github_plan_status.client_test_notes import OPEN_CLIENT_QUESTIONS
        from control_plane.client_test_notes import ClientTestNotesRequired
        from control_plane.workflows.preview_pr_feedback import pull_request_client_review

        payload = {
            "body": f"## Client test notes\nCheck the page.\n### {OPEN_CLIENT_QUESTIONS}\n- What is the address?",
            "labels": [],
        }
        with (
            patch(
                "control_plane.workflows.preview_pr_feedback.launchplane_github_token",
                return_value=nullcontext("fixture"),
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                return_value=payload,
            ),
        ):
            self.assertTrue(
                pull_request_client_review(
                    control_plane_root=Path("unused"),
                    context="testing",
                    anchor_pr_url="https://github.com/example/site/pull/42",
                    label="owner-review",
                )
            )
            self.assertTrue(
                change_client_review(
                    body=str(payload["body"]),
                    labels=(),
                    review_label="owner-review",
                    pure_dependency_update=True,
                ).error
            )
            payload["body"] = "## Client test notes\n"
            with self.assertRaises(ClientTestNotesRequired):
                pull_request_client_review(
                    control_plane_root=Path("unused"),
                    context="testing",
                    anchor_pr_url="https://github.com/example/site/pull/42",
                    label="owner-review",
                )

    def test_release_testing_engineering_marker_keeps_each_sections_review_intent(self) -> None:
        notes = f"{ENGINEERING_RELEASE_TESTS}\nCheck the imported parts at release."
        body = "## Client test notes\n" + notes
        intent = change_client_review(body=body, labels=(), review_label="owner-review")
        self.assertFalse(intent.review_required)
        self.assertFalse(intent.error)
        for held_body, labels in (
            (body, ("owner-review",)),
            (body + "\n## Owner test notes\nApprove the wording before merge.", ()),
            (f"## Client test notes\n### #1\n{notes}\n### #2\nApprove the screen.", ()),
        ):
            with self.subTest(body=held_body, labels=labels):
                self.assertTrue(
                    change_client_review(
                        body=held_body, labels=labels, review_label="owner-review"
                    ).review_required
                )
        self.assertTrue(client_test_notes(ENGINEERING_RELEASE_TESTS).error)

    def test_director_standing_holds_only_for_the_review_label(self) -> None:
        for notes in (
            "",
            "Nothing for the Client to test",
            "Nothing for the Client to test: CI only.",
            "Try the new buttons.",
        ):
            for labels in ((), ("owner-review",)):
                with self.subTest(notes=notes, labels=labels):
                    body = "## Client test notes\n" + notes
                    standing = change_client_review(
                        body=body,
                        labels=labels,
                        review_label="owner-review",
                        director_standing=True,
                    )
                    if labels:
                        self.assertEqual(
                            standing,
                            change_client_review(
                                body=body, labels=labels, review_label="owner-review"
                            ),
                        )
                        self.assertTrue(standing.review_required)
                    else:
                        self.assertFalse(standing.review_required)
                        self.assertEqual(standing.error, client_test_notes(notes).error)
        for labels, review_required in (((), False), (("owner-review",), True)):
            with self.subTest(dependency_update=True, labels=labels):
                intent = change_client_review(
                    body="## Client test notes\nTry the new buttons.",
                    labels=labels,
                    review_label="owner-review",
                    director_standing=True,
                    pure_dependency_update=True,
                )
                self.assertIs(intent.review_required, review_required)
                self.assertFalse(intent.error)

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
            payload["labels"] = [{"name": "owner-review"}]
            self.assertTrue(read(director_standing=True))
            payload["labels"] = []
            self.assertFalse(read(store))
            payload["body"] = "Synthetic train candidate."
            self.assertFalse(read(store))
            with self.assertRaises(ClientTestNotesRequired):
                read()
            with self.assertRaises(ClientTestNotesRequired):
                read(director_standing=True)
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
