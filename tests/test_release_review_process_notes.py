"""Client checklist checks survive removal of agent release-process conditions."""

import unittest

from control_plane.release_review_github import (
    read_release_changes,
    release_test_notes,
    missing_owner_test_notes,
    pull_requests_missing_owner_test_notes,
)
from tests.test_release_review import BASE, HEAD, github_read


class ReleaseProcessNotesTests(unittest.TestCase):
    def test_internal_handoff_condition_is_removed_without_losing_the_check(self) -> None:
        notes = (
            "Check sustained stability, then accept the exact proposed release through "
            "Launchplane after its completed cleanup and memory evidence is recorded."
        )
        displayed, changed = release_test_notes(notes)
        self.assertTrue(changed)
        self.assertIn("Check sustained stability", displayed)
        self.assertNotIn("cleanup", displayed)
        self.assertNotIn("memory evidence", displayed)
        self.assertNotIn("accept the exact proposed release", displayed)

    def test_unrelated_product_instructions_and_safety_limits_are_preserved(self) -> None:
        for notes in (
            "Check that the Accept button saves the order. Then check the order history.",
            "Check the cleanup status on the testing site's jobs page.",
            "Backup retention is still being checked and is not stated in the notice yet.",
            "Check the page. Record what you find in the Launchplane release review.",
            "Nothing for the Client to test: Internal cleanup and memory evidence only.",
            "Check the available memory in Launchplane's service diagnostics.",
            "Approve the release in Launchplane after checking that the memory panel shows data.",
        ):
            with self.subTest(notes=notes):
                self.assertEqual(release_test_notes(notes), (notes, False))

    def test_compiled_item_marks_process_sanitation_and_keeps_source_body(self) -> None:
        notes = (
            "Open the testing site and check the contact form.\n"
            "Then accept the release through Launchplane after cleanup evidence is recorded."
        )
        pulls = github_read("pulls")
        assert isinstance(pulls, list)
        pulls[0]["body"] = "## Client test notes\n" + notes
        original = pulls[0]["body"]
        items, _ = read_release_changes(
            repository="example/site",
            production_commit=BASE,
            candidate_commit=HEAD,
            read=lambda path: github_read(path) if "/compare/" in path else pulls,
        )
        self.assertEqual(pulls[0]["body"], original)
        self.assertTrue(items[0].preview_era_notes)
        self.assertIn("check the contact form", items[0].owner_test_notes)
        self.assertNotIn("cleanup evidence", items[0].owner_test_notes)

    def test_process_only_notes_do_not_become_a_complete_test_instruction(self) -> None:
        displayed, changed = release_test_notes(
            "Accept the release through Launchplane after cleanup and memory evidence is recorded."
        )
        self.assertTrue(changed)
        self.assertEqual(displayed, "")

    def test_unpunctuated_process_line_preserves_following_checks_and_batch_blockers(self) -> None:
        notes = (
            "Check the contact form.\n"
            "Then accept the release through Launchplane after cleanup evidence is recorded\n\n"
            "### #124 Another change\n\n" + missing_owner_test_notes(124) + "\n"
            "Check the checkout total matches the cart."
        )
        displayed, changed = release_test_notes(notes)
        self.assertTrue(changed)
        self.assertIn("Check the contact form.", displayed)
        self.assertIn("Check the checkout total matches the cart.", displayed)
        self.assertEqual(pull_requests_missing_owner_test_notes(displayed), (124,))

    def test_process_list_items_and_urls_leave_no_fragments(self) -> None:
        for prefix in ("", "- ", "2. "):
            with self.subTest(prefix=prefix):
                notes = prefix + (
                    "Accept the release in Launchplane once cleanup is recorded at "
                    "https://control.example.invalid/evidence."
                )
                self.assertEqual(release_test_notes(notes), ("", True))

    def test_url_ends_the_instruction_before_a_following_testing_sentence(self) -> None:
        notes = (
            "Check the page, then accept the release in Launchplane after cleanup is recorded "
            "at https://control.example.invalid/evidence. Check the footer."
        )
        displayed, changed = release_test_notes(notes)
        self.assertTrue(changed)
        self.assertIn("Check the page", displayed)
        self.assertIn("Check the footer.", displayed)

    def test_batch_member_with_only_process_notes_retains_the_missing_notes_gate(self) -> None:
        notes = (
            "### #123 First change\n\nAccept the release in Launchplane after cleanup is recorded.\n\n"
            "### #124 Second change\n\nCheck the checkout total."
        )
        displayed, changed = release_test_notes(notes)
        self.assertTrue(changed)
        self.assertEqual(pull_requests_missing_owner_test_notes(displayed), (123,))
        self.assertIn("Check the checkout total.", displayed)

    def test_following_check_survives_a_conjunction_in_the_same_sentence(self) -> None:
        for tail in (", then check the footer.", " and confirm the banner is gone."):
            with self.subTest(tail=tail):
                notes = (
                    "Accept the release in Launchplane after cleanup evidence is recorded" + tail
                )
                displayed, changed = release_test_notes(notes)
                self.assertTrue(changed)
                self.assertIn(tail.split()[-1], displayed)
                self.assertNotIn("cleanup", displayed)

    def test_headings_and_labels_do_not_stand_in_for_removed_instructions(self) -> None:
        for heading in ("Test steps:", "#### Steps"):
            with self.subTest(heading=heading):
                notes = heading + "\nAccept the release in Launchplane after cleanup is recorded."
                self.assertEqual(release_test_notes(notes), ("", True))
                displayed, _ = release_test_notes("### #123 Change\n" + notes)
                self.assertEqual(pull_requests_missing_owner_test_notes(displayed), (123,))

    def test_process_examples_in_fences_are_data(self) -> None:
        notes = "```text\nAccept the release in Launchplane after cleanup is recorded.\n```"
        self.assertEqual(release_test_notes(notes), (notes, False))
